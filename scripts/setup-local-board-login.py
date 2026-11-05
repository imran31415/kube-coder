"""Interactive Windows/WSL credential entry. No secrets in argv, logs or the repo.

Register a GitHub OAuth app with callback http://localhost:7070/oauth2/callback,
then run this from an interactive terminal. Authority services run as nobody;
coding agents run as the normal WSL user. This does not harden the host itself.
"""
import getpass
import json
import subprocess


def main():
    print('GitHub OAuth callback: http://localhost:7070/oauth2/callback')
    client_id = input('OAuth client ID: ').strip()
    client_secret = getpass.getpass('OAuth client secret (hidden): ').strip()
    login = input('Allowed GitHub login [DevolRaman750]: ').strip() or 'DevolRaman750'
    if not client_id or not client_secret or not login:
        raise SystemExit('Client ID, secret and login are required.')
    # Fixed paths only. JSON travels over stdin, never shell interpolation.
    provision = '''
import base64, json, os, secrets, sys
data = json.load(sys.stdin)
root = '/var/lib/kube-coder-board-auth'
os.makedirs(root, mode=0o700, exist_ok=True)
os.chmod(root, 0o700)
os.chown(root, 65534, 65534)
settings = dict(provider='github', client_id=data['client_id'], client_secret=data['client_secret'],
    cookie_secret=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    http_address='127.0.0.1:7070', redirect_url='http://localhost:7070/oauth2/callback',
    upstreams=['http://127.0.0.1:7071/'], email_domains=['*'], github_users=[data['login']],
    cookie_secure=False, cookie_samesite='lax', cookie_httponly=True,
    set_xauthrequest=True, pass_user_headers=True, pass_access_token=False,
    skip_provider_button=True)
path = root + '/oauth2-proxy.cfg'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as f:
    for key, value in settings.items():
        f.write(key + ' = ' + json.dumps(value) + '\\n')
os.chmod(path, 0o600)
os.chown(path, 65534, 65534)
secret = root + '/control-secret'
if not os.path.exists(secret):
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f: f.write(secrets.token_urlsafe(32))
    os.chown(secret, 65534, 65534)
print('Saved private OAuth configuration for the isolated approval services.')
'''
    subprocess.run(['wsl', '-d', 'Ubuntu', '-u', 'root', '--', 'python3', '-c', provision],
                   input=json.dumps(dict(client_id=client_id, client_secret=client_secret, login=login)),
                   text=True, check=True)


if __name__ == '__main__':
    main()
