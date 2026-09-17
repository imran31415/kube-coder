"""Mobile push notifications (Expo) — device-token store + dispatch.

The mobile app (see `mobile/`) had no OS push: alerts only "rode the feed and
in-app polling" (docs/boards.md). This module closes that gap. It is the server
half of the feature:

1. **Token store.** Devices register their Expo push token via
   `POST /api/push/register`; tokens persist on the PVC at
   `/home/dev/.claude-push/tokens.json` so they survive pod restarts. Writes are
   serialised with an `flock`ed `.lock` sidecar + tmp/rename, the same discipline
   as `boards/store.py:JsonRecord` and `ClaudeTaskManager._atomic_update_meta`.
   Kept self-contained (no `boards` import) so push works even when the boards
   package is unavailable, and so it is unit-testable in isolation.

2. **Dispatch.** `dispatch(item)` is called from `FeedManager.emit()` for every
   feed item. It pushes only the high-signal ones — `waiting=True` (an agent is
   blocked on the human) or `kind='decision'` — to avoid notification fatigue.
   Delivery is fire-and-forget on a daemon thread with a short timeout, mirroring
   `ClaudeTaskManager._fire_completion_hook`: a slow or failing `exp.host` must
   never stall a request handler, errors are logged to stderr and swallowed, and
   there are no retries. Tokens Expo reports as `DeviceNotRegistered` are pruned.

3. **Repeats (#685).** The feed coalesces by `dedupe_key`, so a task that flips
   waiting → running → waiting keeps updating ONE row — but it used to push on
   every flip, and 76% of pushes were copies the phone already had. `PushLedger`
   (`sent.json`, next to the tokens) remembers what was last pushed per key: a
   repeat pushes only once the user has read the row since that push (or the
   row was dismissed, so this is a new one), and never more often than
   `KC_PUSH_MIN_INTERVAL`. Each message also carries `collapseId`/`tag` = the
   feed id, so a repeat that does go out replaces the earlier notification on
   the phone instead of stacking beside it, and a `ttl` so a stale alert is
   dropped rather than delivered hours late.

The whole feature is gated by `KC_PUSH_ENABLED` (default on). `EXPO_ACCESS_TOKEN`
is optional (Expo's push API needs no key unless the project enables enhanced
security). `KC_PUSH_DIR` moves the token/ledger directory — for tests and
throwaway runs, never a deployment (it would forget every registered phone).
"""

import fcntl
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

# ── config ───────────────────────────────────────────────────────────────────

# The deployment path. Every workspace's registered phones already live here.
DEFAULT_PUSH_DIR = '/home/dev/.claude-push'
# A repeat of the same alert pushes at most this often (seconds).
DEFAULT_MIN_INTERVAL = 1800


def _resolve_push_dir(env=None):
    """`$KC_PUSH_DIR`, or the deployment default when unset/blank — the same
    blank-means-default contract as `$KC_MEMORY_DB` in memory/store.py."""
    src = os.environ if env is None else env
    return ((src.get('KC_PUSH_DIR') or '').strip()) or DEFAULT_PUSH_DIR


def _resolve_min_interval(env=None):
    """`$KC_PUSH_MIN_INTERVAL` in seconds. Blank, unparseable or negative falls
    back to the default rather than to "no limit"; `0` switches the timer off
    (the read-since-last-push rule still applies)."""
    src = os.environ if env is None else env
    raw = (src.get('KC_PUSH_MIN_INTERVAL') or '').strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MIN_INTERVAL
    if value != value or value < 0:  # NaN or negative
        return DEFAULT_MIN_INTERVAL
    return value


# Read as module globals so tests can monkeypatch push_notify.PUSH_ENABLED etc.
PUSH_DIR = _resolve_push_dir()
TOKENS_PATH = PUSH_DIR + '/tokens.json'
SENT_PATH = PUSH_DIR + '/sent.json'
EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'

PUSH_ENABLED = os.environ.get('KC_PUSH_ENABLED', 'true').lower() == 'true'
EXPO_ACCESS_TOKEN = os.environ.get('EXPO_ACCESS_TOKEN', '').strip()
MIN_INTERVAL = _resolve_min_interval()

# An alert older than this is dropped by Expo/APNs/FCM rather than delivered
# late: "waiting on you" from an hour ago is noise, and a phone that was offline
# would otherwise receive the backlog as one burst.
PUSH_TTL_SECONDS = 3600
# Ledger entries older than this are forgotten on the next write.
_SENT_RETENTION = 7 * 24 * 3600
# APNs caps apns-collapse-id at 64 bytes.
_COLLAPSE_ID_MAX = 64
# The ledger's clock — a module global so tests can move time forward.
_clock = time.time

# Expo tokens look like ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx] (or ExpoPushToken[...]).
_TOKEN_PREFIXES = ('ExponentPushToken[', 'ExpoPushToken[')
_DISPATCH_TIMEOUT = 10


def is_expo_token(token):
    """Cheap shape check so we never persist obvious junk. Expo validates fully
    on send; this just rejects the accidental empty string / wrong field."""
    return (isinstance(token, str)
            and any(token.startswith(p) for p in _TOKEN_PREFIXES)
            and token.endswith(']')
            and len(token) <= 256)


# ── locked JSON files ────────────────────────────────────────────────────────

def _read_json(path):
    """The parsed file, or None when it is missing or not valid JSON."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path, data):
    """Atomic tmp/rename write of a private (0600) file."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _locked_update(path, lock, read, mutate):
    """Read-modify-write `path` under `lock` (in-process) and an flock on a
    `.lock` sidecar (across processes). `mutate` may return False to skip the
    write when it changed nothing."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with lock, open(path + '.lock', 'a') as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            data = read()
            if mutate(data) is not False:
                _write_json(path, data)
            return data
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


# ── token store ──────────────────────────────────────────────────────────────

class PushTokenStore:
    """`{ "tokens": { "<expo_token>": {owner, platform, ts} } }` on the PVC.

    Keyed by the token itself so a re-register is an idempotent upsert rather
    than a duplicate. `owner` is the caller's `_memory_actor()` string, kept for
    debugging/audit; dispatch is pod-wide (single-operator model) so lookup does
    not filter by owner.
    """

    _lock = threading.Lock()  # in-process; the flock guards across processes

    @staticmethod
    def _read_unlocked():
        data = _read_json(TOKENS_PATH)
        if not isinstance(data, dict) or not isinstance(data.get('tokens'), dict):
            return {'tokens': {}}
        return data

    @staticmethod
    def _update(mutate):
        return _locked_update(TOKENS_PATH, PushTokenStore._lock,
                              PushTokenStore._read_unlocked, mutate)

    @staticmethod
    def register(token, platform, owner):
        def mut(data):
            data['tokens'][token] = {
                'owner': owner or 'unknown',
                'platform': platform or '',
                'ts': time.time(),
            }
        PushTokenStore._update(mut)

    @staticmethod
    def unregister(token):
        removed = {'hit': False}

        def mut(data):
            removed['hit'] = data['tokens'].pop(token, None) is not None
        PushTokenStore._update(mut)
        return removed['hit']

    @staticmethod
    def prune(tokens):
        """Drop tokens Expo reported as permanently invalid."""
        drop = set(tokens)
        if not drop:
            return

        def mut(data):
            for t in drop:
                data['tokens'].pop(t, None)
        PushTokenStore._update(mut)

    @staticmethod
    def all_tokens():
        return list(PushTokenStore._read_unlocked().get('tokens', {}).keys())


# ── sent ledger ──────────────────────────────────────────────────────────────

def _may_repeat(entry, item_id, seen, now):
    """Whether an alert whose key has a ledger `entry` may push again (#685).

    In order: a key never pushed goes out; inside the minimum interval nothing
    does; a different row (the old one was dismissed or rotated away) is new
    news; the same row goes out again only if the user read it since the last
    push. A clock that stepped backwards does not hold the key hostage."""
    if not isinstance(entry, dict):
        return True
    last = entry.get('ts')
    if isinstance(last, (int, float)) and 0 <= now - last < MIN_INTERVAL:
        return False
    if entry.get('item_id') != item_id:
        return True
    return bool(seen)


class PushLedger:
    """`{ "keys": { "<dedupe_key>": {item_id, ts} } }` — the last push per feed
    `dedupe_key`, kept on the PVC so a pod restart does not re-page the phone.

    Only keyed items are tracked: an item without a `dedupe_key` is its own
    event and always pushes. Entries older than `_SENT_RETENTION` are dropped
    on the next write, so the file stays small.
    """

    _lock = threading.Lock()  # in-process; the flock guards across processes

    @staticmethod
    def _read_unlocked():
        data = _read_json(SENT_PATH)
        if not isinstance(data, dict) or not isinstance(data.get('keys'), dict):
            return {'keys': {}}
        return data

    @staticmethod
    def claim(key, item_id, seen, now=None):
        """Decide whether this emit of `key` may push and, if so, record it — in
        one locked read-modify-write, so two emits racing on the same key cannot
        both win. Returns True when the caller should push."""
        now = _clock() if now is None else now
        verdict = {'push': False}

        def mutate(data):
            keys = data['keys']
            if not _may_repeat(keys.get(key), item_id, seen, now):
                return False
            verdict['push'] = True
            keys[key] = {'item_id': item_id, 'ts': now}
            cutoff = now - _SENT_RETENTION
            for k in [k for k, e in keys.items()
                      if not isinstance(e, dict)
                      or not isinstance(e.get('ts'), (int, float))
                      or e['ts'] < cutoff]:
                del keys[k]
            return True

        _locked_update(SENT_PATH, PushLedger._lock, PushLedger._read_unlocked, mutate)
        return verdict['push']

    @staticmethod
    def entries():
        return dict(PushLedger._read_unlocked()['keys'])


# ── dispatch ─────────────────────────────────────────────────────────────────

def should_push(item):
    """High-signal only: an agent is blocked on the human (`waiting`), or a
    decision was recorded. Everything else stays in the feed. Kept as one small
    predicate so a maintainer can widen it in one place."""
    if not isinstance(item, dict):
        return False
    return bool(item.get('waiting')) or item.get('kind') == 'decision'


def _build_messages(item, tokens):
    """One Expo message per registered device. `data.ref` carries the feed
    item's primary deep-link so a tap routes to the right screen (the mobile
    side maps it through resolveFeedRef); falls back to opening the Feed."""
    links = item.get('links') or []
    ref = ''
    for lk in links:
        if isinstance(lk, dict) and lk.get('ref'):
            ref = lk['ref']
            break
    title = (item.get('title') or 'kube-coder').strip()[:120]
    body = (item.get('body_md') or '').strip().splitlines()
    body = (body[0] if body else ('Action needed' if item.get('waiting') else '')).strip()[:180]
    feed_id = item.get('id') or ''
    data = {'ref': ref, 'feedId': feed_id, 'kind': item.get('kind') or '',
            'waiting': bool(item.get('waiting'))}
    msg = {
        'to': None,  # filled per token below
        'title': title,
        'body': body,
        'data': data,
        'sound': 'default',
        'priority': 'high',
        'channelId': 'default',
        'ttl': PUSH_TTL_SECONDS,
    }
    # The feed id is stable across coalesced re-emits, so a repeat replaces the
    # notification already on the phone: `collapseId` does it on iOS, `tag` on
    # Android (where collapseId only merges messages still in transit). A
    # dismissed row gets a new id, so its next alert arrives as a fresh one.
    if feed_id and len(feed_id.encode('utf-8')) <= _COLLAPSE_ID_MAX:
        msg['collapseId'] = feed_id
        msg['tag'] = feed_id
    out = []
    for t in tokens:
        m = dict(msg)
        m['to'] = t
        out.append(m)
    return out


def _post_expo(messages):
    payload = json.dumps(messages).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
               'Accept-Encoding': 'identity'}
    if EXPO_ACCESS_TOKEN:
        headers['Authorization'] = f'Bearer {EXPO_ACCESS_TOKEN}'
    req = urllib.request.Request(EXPO_PUSH_URL, data=payload, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=_DISPATCH_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _deliver(messages):
    """Runs on a daemon thread. Sends the batch and prunes dead tokens. Any
    failure is logged and swallowed — a push that doesn't land must never affect
    the fact it was announcing."""
    try:
        result = _post_expo(messages)
    except Exception as e:  # pragma: no cover - network path
        print(f'[push] dispatch failed: {e}', file=sys.stderr)
        return
    # Response `data` is a receipt list aligned with the messages we sent.
    receipts = result.get('data') if isinstance(result, dict) else None
    if not isinstance(receipts, list):
        return
    dead = []
    for msg, receipt in zip(messages, receipts):
        if not isinstance(receipt, dict) or receipt.get('status') != 'error':
            continue
        details = receipt.get('details') or {}
        if details.get('error') == 'DeviceNotRegistered':
            dead.append(msg['to'])
    if dead:
        try:
            PushTokenStore.prune(dead)
        except Exception as e:  # pragma: no cover - defensive
            print(f'[push] prune failed: {e}', file=sys.stderr)


def dispatch(item, *, seen=False):
    """Fire-and-forget push for a feed item. Cheap and safe to call for EVERY
    emit: returns False immediately unless the item is high-signal, push is
    enabled, a device is registered and — for a keyed item — the ledger allows
    it. Returns True when a push was queued.

    `seen` is whether the user had read this item's row before this emit; it is
    what lets a repeat through once the last alert was acted on.

    The ledger is consulted only after the token check, so nothing is recorded
    while no phone is registered and a phone that registers later still hears
    the next repeat. If the ledger itself fails, the push goes out anyway: a
    duplicate is a nuisance, a lost "waiting on you" is not."""
    if not PUSH_ENABLED or not should_push(item):
        return False
    try:
        tokens = PushTokenStore.all_tokens()
    except Exception as e:  # pragma: no cover - defensive
        print(f'[push] token read failed: {e}', file=sys.stderr)
        return False
    if not tokens:
        return False
    key = item.get('dedupe_key') or ''
    if key:
        try:
            if not PushLedger.claim(key, item.get('id') or '', seen):
                return False
        except Exception as e:
            print(f'[push] ledger failed, pushing anyway: {e}', file=sys.stderr)
    messages = _build_messages(item, tokens)
    threading.Thread(target=_deliver, args=(messages,), daemon=True).start()
    return True
