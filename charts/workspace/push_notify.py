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

The whole feature is gated by `KC_PUSH_ENABLED` (default on). `EXPO_ACCESS_TOKEN`
is optional (Expo's push API needs no key unless the project enables enhanced
security).
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

PUSH_DIR = '/home/dev/.claude-push'
TOKENS_PATH = PUSH_DIR + '/tokens.json'
EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'

# Read as module globals so tests can monkeypatch push_notify.PUSH_ENABLED etc.
PUSH_ENABLED = os.environ.get('KC_PUSH_ENABLED', 'true').lower() == 'true'
EXPO_ACCESS_TOKEN = os.environ.get('EXPO_ACCESS_TOKEN', '').strip()

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
    def _ensure_dir():
        os.makedirs(PUSH_DIR, mode=0o700, exist_ok=True)

    @staticmethod
    def _read_unlocked():
        try:
            with open(TOKENS_PATH) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {'tokens': {}}
        if not isinstance(data, dict) or not isinstance(data.get('tokens'), dict):
            return {'tokens': {}}
        return data

    @staticmethod
    def _write_unlocked(data):
        PushTokenStore._ensure_dir()
        fd, tmp = tempfile.mkstemp(dir=PUSH_DIR, prefix='.tmp-', suffix='.json')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, TOKENS_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _update(mutate):
        """Read-modify-write under an exclusive cross-process lock."""
        PushTokenStore._ensure_dir()
        lock_path = TOKENS_PATH + '.lock'
        with PushTokenStore._lock, open(lock_path, 'a') as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                data = PushTokenStore._read_unlocked()
                mutate(data)
                PushTokenStore._write_unlocked(data)
                return data
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)

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
    data = {'ref': ref, 'feedId': item.get('id') or '', 'kind': item.get('kind') or '',
            'waiting': bool(item.get('waiting'))}
    msg = {
        'to': None,  # filled per token below
        'title': title,
        'body': body,
        'data': data,
        'sound': 'default',
        'priority': 'high',
        'channelId': 'default',
    }
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


def dispatch(item):
    """Fire-and-forget push for a feed item. Cheap and safe to call for EVERY
    emit: returns immediately unless the item is high-signal, push is enabled,
    and at least one device is registered."""
    if not PUSH_ENABLED or not should_push(item):
        return
    try:
        tokens = PushTokenStore.all_tokens()
    except Exception as e:  # pragma: no cover - defensive
        print(f'[push] token read failed: {e}', file=sys.stderr)
        return
    if not tokens:
        return
    messages = _build_messages(item, tokens)
    threading.Thread(target=_deliver, args=(messages,), daemon=True).start()
