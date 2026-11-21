#!/usr/bin/env python3
"""Ask the controller to clean up a Dex identity after confirmed teardown."""
import json
import os
import re
import sys
import urllib.request


def main():
    slug = sys.argv[1] if len(sys.argv) == 2 else ''
    if not re.fullmatch(r'[a-z0-9](?:-?[a-z0-9]){0,38}', slug):
        raise SystemExit('Invalid workspace name.')
    origin = os.environ.get('KC_CONTROLLER_URL', '').rstrip('/')
    token = os.environ.get('KC_CONTROLLER_ADMIN_TOKEN', '')
    if not origin and not token:
        print('Dex cleanup will be reconciled by the controller after namespace deletion.')
        return
    if not origin.startswith('https://') or not token:
        raise SystemExit('Set both KC_CONTROLLER_URL (HTTPS) and KC_CONTROLLER_ADMIN_TOKEN.')
    request = urllib.request.Request(origin + '/oauth/api/provision/' + slug + '/identity',
        method='DELETE', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        if result.get('ok') is not True:
            raise ValueError()
    except (OSError, ValueError):
        raise SystemExit('INCOMPLETE: identity cleanup was not confirmed. Check controller logs and retry.') from None
    print('Dex identity cleanup complete.')


if __name__ == '__main__':
    main()
