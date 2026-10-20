"""What the workspace tells the keeper (#728 step 4).

The keeper runs outside the pod and has to answer two questions while the
workspace is asleep and cannot be asked:

  * is this webhook/cron/page-watch id real?  — if not, answer 404 and wake
    nothing, so a stranger walking URLs cannot start a 4 GiB pod;
  * is this bearer the workspace's API token?  — so the phone app can wake it.

Both answers are pushed from here, and neither is a secret. The trigger
projection is ids and a provider name; the token goes as a SHA-256 hash, which
is enough to check a token and not enough to mint one. There is a test that
asserts no secret ever appears in what this module sends, and it is the most
important test in the file.

Shape follows keeper_idle.py: the collectors take their directories as
arguments rather than importing server.py, so they are testable on their own
and there is no import cycle.

Everything no-ops when KEEPER_CONTROL_URL is unset, which is every deployment
without a keeper.
"""

import hashlib
import json
import os
import sys
import threading
import time
import urllib.parse

import safe_http

# The only provider names the keeper is told about, mirroring
# WebhookManager.PROVIDERS. An unrecognised value is dropped rather than passed
# on: the keeper uses it to pick a queue TTL, and a value it does not know would
# just be a guess.
PROVIDERS = ('generic', 'github', 'slack', 'stripe')

# Kinds, mirroring TriggerRunsManager.KINDS (server.py).
KIND_WEBHOOK = 'webhook'
KIND_CRON = 'cron'
KIND_PAGE_WATCH = 'page-watch'

# A push is tiny and the keeper is one hop away, so the timeout is short and the
# retries are few: the point of retrying at all is to survive a keeper restart
# or a rolling update, not a long outage. A failed push self-heals at the
# workspace's next boot, which pushes everything again.
TIMEOUT_S = 5
MAX_ATTEMPTS = 4


def _ids_in(directory):
    """Ids of the trigger configs in `directory`, from their filenames.

    The filename is the id (the managers write `<id>.json`), so this never
    opens a file — the cheapest projection is also the one that cannot leak.
    An unreadable directory yields nothing, which makes the keeper answer 404:
    the fail-safe direction, matching keeper_idle's rule that an unreadable
    source must never read as permission.
    """
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [n[:-len('.json')] for n in names if n.endswith('.json')]


def _provider_of(directory, webhook_id):
    """The webhook's provider, or '' when it cannot be established.

    This is the only value this module ever reads out of a trigger config, and
    it is checked against an allowlist before being returned. Keeping the read
    this narrow is what makes "no secret leaves the workspace" a property of
    the code rather than a promise about it.
    """
    try:
        with open(os.path.join(directory, webhook_id + '.json'), 'r') as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return ''
    if not isinstance(cfg, dict):
        return ''
    provider = cfg.get('provider')
    return provider if provider in PROVIDERS else ''


def collect_triggers(webhooks_dir, crons_dir, page_watches_dir):
    """Every trigger the workspace owns, as {kind, id, provider}.

    The whole set, every time. The keeper replaces what it holds with this, so
    a delete that raced a keeper restart cannot leave a deleted id behind — and
    a deleted webhook that still wakes the workspace is exactly the bug this
    design exists to prevent.
    """
    out = []
    for wid in _ids_in(webhooks_dir):
        entry = {'kind': KIND_WEBHOOK, 'id': wid}
        provider = _provider_of(webhooks_dir, wid)
        if provider:
            entry['provider'] = provider
        out.append(entry)
    for kind, directory in ((KIND_CRON, crons_dir), (KIND_PAGE_WATCH, page_watches_dir)):
        for tid in _ids_in(directory):
            out.append({'kind': kind, 'id': tid})
    return out


def token_hash(token):
    """The SHA-256 of the API token, as lowercase hex — what the keeper stores.

    The keeper can check a presented token against this and cannot turn it back
    into one, which is the whole reason the hash is what travels.
    """
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


class KeeperPush:
    """The client. All of it is a no-op without KEEPER_CONTROL_URL."""

    @staticmethod
    def control_url():
        """The keeper's control listener, or '' when there is no keeper.

        Read per call rather than at import so a test can set it, and so the
        value always reflects the pod's current environment.
        """
        return (os.environ.get('KEEPER_CONTROL_URL') or '').strip().rstrip('/')

    @staticmethod
    def _secret():
        return (os.environ.get('KEEPER_CONTROL_SECRET') or '').strip()

    @staticmethod
    def enabled():
        return bool(KeeperPush.control_url() and KeeperPush._secret())

    @staticmethod
    def _post(path, payload):
        """POST one push, with bounded backoff. Returns True on success.

        `allow_internal=True` is deliberate and narrow. The keeper is a
        ClusterIP, so safe_http's public-address rule would refuse it; the
        exception is passed explicitly here rather than by threading the
        ALLOW_INTERNAL_HOOKS global through, because that global is about
        user-supplied hook URLs and this URL comes from the pod spec.
        """
        base = KeeperPush.control_url()
        if not base:
            return False
        secret = KeeperPush._secret()
        if not secret:
            print('[keeper-push] KEEPER_CONTROL_URL is set but KEEPER_CONTROL_SECRET is not; '
                  'not pushing', file=sys.stderr)
            return False
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            print(f'[keeper-push] KEEPER_CONTROL_URL is not a URL: {base!r}', file=sys.stderr)
            return False

        body = json.dumps(payload).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {secret}',
        }
        last = ''
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                status, _, resp = safe_http.fetch(
                    base + path, method='POST', headers=headers, body=body,
                    timeout=TIMEOUT_S, allow_internal=True)
                if 200 <= status < 300:
                    return True
                last = f'HTTP {status}'
                # The keeper rejecting the content will reject it again; only a
                # 5xx or a transport failure is worth another attempt.
                if 400 <= status < 500:
                    print(f'[keeper-push] {path} refused: {last} {resp[:200]!r}', file=sys.stderr)
                    return False
            except Exception as e:  # transport, SSRF guard, anything
                last = f'{type(e).__name__}: {e}'
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
        print(f'[keeper-push] {path} failed after {MAX_ATTEMPTS} attempts: {last}. '
              'The keeper keeps what it last knew; the next workspace boot pushes again.',
              file=sys.stderr)
        return False

    @staticmethod
    def push_triggers_now(webhooks_dir, crons_dir, page_watches_dir):
        """Collect and push. Synchronous; returns True on success."""
        if not KeeperPush.enabled():
            return False
        triggers = collect_triggers(webhooks_dir, crons_dir, page_watches_dir)
        return KeeperPush._post('/api/keeper/triggers', {'triggers': triggers})

    @staticmethod
    def push_token_hash_now(token):
        """Push the hash of `token`. Synchronous; returns True on success."""
        if not KeeperPush.enabled():
            return False
        return KeeperPush._post('/api/keeper/token-hash', {'sha256': token_hash(token)})

    @staticmethod
    def _in_thread(name, fn, *args):
        """Run fn off the request path. A push must never make a user wait, and
        must never turn a keeper problem into a failed trigger edit."""
        if not KeeperPush.enabled():
            return
        t = threading.Thread(target=fn, args=args, name=name, daemon=True)
        t.start()

    @staticmethod
    def push_triggers(webhooks_dir, crons_dir, page_watches_dir):
        """Fire-and-forget trigger push, for a create or a delete.

        Pushes are idempotent — each one replaces the keeper's whole set — so
        several in quick succession are harmless and need no debouncing.
        """
        KeeperPush._in_thread('keeper-push-triggers', KeeperPush.push_triggers_now,
                              webhooks_dir, crons_dir, page_watches_dir)

    @staticmethod
    def push_token_hash(token):
        """Fire-and-forget token-hash push, for a rotation."""
        KeeperPush._in_thread('keeper-push-token', KeeperPush.push_token_hash_now, token)

    @staticmethod
    def start(token, webhooks_dir, crons_dir, page_watches_dir):
        """The boot push, in a daemon thread.

        At boot because the keeper may have restarted, or been deployed for the
        first time, or missed a push while it was down, while the workspace
        slept and could not be asked. This is also the only thing that recovers
        a keeper whose store was corrupt — so it must run unconditionally, not
        only when something changed.
        """
        if not KeeperPush.enabled():
            return

        def _boot():
            # The token hash first: it is what lets the app wake the workspace,
            # and it is one small request.
            KeeperPush.push_token_hash_now(token)
            KeeperPush.push_triggers_now(webhooks_dir, crons_dir, page_watches_dir)

        threading.Thread(target=_boot, name='keeper-push-boot', daemon=True).start()
