"""Mint a Zendesk OAuth access token with PKCE, in two runnable halves.

The Zendesk connector template authenticates with an OAuth **access token**,
because API tokens are no longer obtainable: Zendesk withdrew their creation
from the admin UI and retires the existing ones on 2027-04-30. Minting an
access token is a browser flow, not a settings page, so there is nothing to
copy out of an admin screen and this script exists to close that gap.

Two things about Zendesk's OAuth that cost an afternoon to discover, recorded
here so nobody rediscovers them:

* Clients are **Public** or **Confidential**, and the admin UI creates Public
  ones. A Public client is permitted **only** the authorization-code + PKCE
  flow — the implicit grant (`response_type=token`) that every integration
  guide still shows is refused, in a way that reads like a misconfigured
  client rather than an unavailable grant.
* A client showing status **Inactive** is not a gate. It means no token has
  been issued yet, and it flips on its own once one is.

PKCE needs no client secret — that is the point of it — so nothing long-lived
is typed into this script.

Split into two commands because the middle step is a browser click in a
session only the human has:

    python3 boards/zendesk_oauth.py start acme 1a2b3c http://localhost
    #   → prints a URL. Open it, approve, and copy the `code` from the
    #     redirect (the browser will fail to load it; that is expected).
    python3 boards/zendesk_oauth.py finish <code-or-the-whole-redirect-url>

`finish` prints the access token once, for pasting into
**Board → Credentials → ZENDESK_OAUTH_TOKEN** (format "token"). It is not
written anywhere: a token on disk in a file this script chose is a token
nobody remembers to delete.

Codes expire in about a minute, so do the two steps back to back.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

SCOPE = 'read write'

# The verifier lives between the two commands. In the user's home rather than
# a shared temp dir, mode 0600: it is single-use and bound to one code, but it
# is still half of an exchange and there is no reason to leave it readable.
STATE_PATH = os.path.join(os.path.expanduser('~'), '.kube-coder-zendesk-pkce')


def _b64(raw):
    """base64url with the padding stripped, which is what RFC 7636 wants."""
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _write_state(state):
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        f.write(json.dumps(state))
    try:
        os.chmod(STATE_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass                       # Windows; the ACL is the user's already


def start(subdomain, client_id, redirect):
    verifier = _b64(secrets.token_bytes(64))          # 86 chars, within 43-128
    challenge = _b64(hashlib.sha256(verifier.encode('ascii')).digest())
    _write_state({'v': verifier, 'sub': subdomain,
                  'cid': client_id, 'redirect': redirect})

    url = (f'https://{subdomain}.zendesk.com/oauth/authorizations/new?'
           + urllib.parse.urlencode({
               'response_type': 'code',
               'client_id': client_id,
               'redirect_uri': redirect,
               'scope': SCOPE,
               'code_challenge': challenge,
               'code_challenge_method': 'S256',
           }))
    print('Open this, approve, then copy the `code` from the address bar:\n')
    print(url)
    print('\nThen: python3 boards/zendesk_oauth.py finish <code>')


def finish(code):
    if not os.path.exists(STATE_PATH):
        sys.exit('No pending exchange — run `start` first.')
    with open(STATE_PATH, encoding='utf-8') as f:
        state = json.load(f)

    # Accept the whole redirect URL as well as a bare code. Copying the address
    # bar is what a person actually does, and picking the code out of it by eye
    # is an easy thing to get subtly wrong.
    if code.startswith('http') or 'code=' in code:
        parsed = urllib.parse.urlparse(code)
        found = urllib.parse.parse_qs(parsed.query or parsed.fragment).get('code')
        if not found:
            sys.exit('No `code` parameter in that. Paste the redirect URL or '
                     'the code itself.')
        code = found[0]

    body = json.dumps({
        'grant_type': 'authorization_code',
        'code': code,
        'client_id': state['cid'],
        'redirect_uri': state['redirect'],
        'code_verifier': state['v'],
        'scope': SCOPE,
    }).encode('utf-8')
    req = urllib.request.Request(
        f'https://{state["sub"]}.zendesk.com/oauth/tokens',
        data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'Accept': 'application/json'})
    try:
        payload = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode('utf-8', 'replace')
        print(f'HTTP {e.code}\n{detail}\n', file=sys.stderr)
        # The two failures worth naming, because neither error string says
        # which one it is.
        print('invalid_grant   → the code was already used or expired (they '
              'last about a minute). Re-run `start` and be quicker.\n'
              'invalid_request → the redirect URL here does not match the one '
              'registered on the OAuth client, character for character.',
              file=sys.stderr)
        sys.exit(1)
    except Exception as e:                                    # noqa: BLE001
        sys.exit(f'{type(e).__name__}: {e}')

    token = payload.get('access_token') or ''
    if not token:
        sys.exit(f'No access_token in the reply: {json.dumps(payload)[:300]}')

    os.remove(STATE_PATH)                     # single use, so do not keep it
    print(f'scope: {payload.get("scope")!r}\n')
    print('Paste this into Board → Credentials as ZENDESK_OAUTH_TOKEN '
          '(type "token"):\n')
    print(token)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Mint a Zendesk OAuth access token with PKCE.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('start', help='print the authorization URL')
    s.add_argument('subdomain', help='the label only: "acme" for '
                                     'acme.zendesk.com')
    s.add_argument('client_id', help='the OAuth client\'s Unique identifier '
                                     '(Admin Center → APIs → OAuth clients)')
    s.add_argument('redirect', help='a redirect URL registered on that client, '
                                    'e.g. http://localhost')

    f = sub.add_parser('finish', help='exchange the code for a token')
    f.add_argument('code', help='the `code` parameter, or the whole redirect '
                                'URL you were sent to')

    args = ap.parse_args(argv)
    if args.cmd == 'start':
        start(args.subdomain, args.client_id, args.redirect)
    else:
        finish(args.code.strip())


if __name__ == '__main__':
    main()
