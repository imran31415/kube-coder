"""Feed, and the mobile push that delivers it (#100, #469).

Seven routes on two verbs. Push lives here rather than in its own module
because it is the Feed's delivery arm, not a separate surface: the device
registry exists so `FeedManager.emit` can page a phone, and `push_notify` is
hooked into that emit path in server.py. Splitting them would put two halves
of one feature in two files.
"""

import json
import re
import sys
import urllib.parse

import handlers
import push_notify
from handlers.routing import RouteTable


class FeedRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def handle_feed_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            since = float(qs['since'][0]) if qs.get('since') else None
        except (ValueError, IndexError):
            since = None
        project = (qs.get('project') or [''])[0] or None
        kinds = [k for k in (qs.get('kinds') or [''])[0].split(',') if k] or None
        unread_only = (qs.get('unread') or [''])[0] in ('1', 'true')
        try:
            limit = int((qs.get('limit') or ['50'])[0])
        except ValueError:
            limit = 50
        self.send_json({'items': handlers.server.FeedManager.list(
            since=since, project=project, kinds=kinds,
            unread_only=unread_only, limit=limit)})

    def handle_feed_unread_count(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'count': handlers.server.FeedManager.unread_count()})

    def handle_feed_create(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        kind = (data.get('kind') or '').strip()
        title = (data.get('title') or '').strip()
        if not title:
            self.send_json({'error': 'title is required'}, 400)
            return
        links = data.get('links')
        if links is not None and not isinstance(links, list):
            self.send_json({'error': 'links must be a list'}, 400)
            return
        item = handlers.server.FeedManager.emit(
            kind, title,
            body_md=(data.get('body_md') or ''),
            source=(data.get('source') or 'agent'),
            project_id=(data.get('project_id') or ''),
            links=links or [],
            waiting=bool(data.get('waiting')),
            dedupe_key=(data.get('dedupe_key') or None),
        )
        if item is None:
            self.send_json({'error': f'kind must be one of {handlers.server.FeedManager.KINDS}'}, 400)
            return
        self.send_json(item, 201)

    def handle_feed_read(self, item_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not handlers.server.FeedManager.mark_read(item_id):
            self.send_json({'error': 'Feed item not found'}, 404)
            return
        self.send_json({'ok': True})

    def handle_feed_dismiss(self, item_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not handlers.server.FeedManager.dismiss(item_id):
            self.send_json({'error': 'Feed item not found'}, 404)
            return
        self.send_json({'ok': True})

    def handle_push_register(self):
        """Register a device's Expo push token (mobile app, after onboarding /
        on cold start). Idempotent upsert keyed by the token."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        token = (data.get('token') or '').strip()
        if not push_notify.is_expo_token(token):
            self.send_json({'error': 'a valid Expo push token is required'}, 400)
            return
        platform = (data.get('platform') or '').strip().lower()
        if platform not in ('ios', 'android', ''):
            self.send_json({'error': "platform must be 'ios' or 'android'"}, 400)
            return
        try:
            push_notify.PushTokenStore.register(token, platform, self._memory_actor())
        except Exception as e:
            print(f'[push] register failed: {e}', file=sys.stderr)
            self.send_json({'error': 'could not store token'}, 500)
            return
        self.send_json({'ok': True}, 201)

    def handle_push_unregister(self):
        """Drop a device token (mobile logout / disconnect). Token via ?token=
        query param or JSON body. Idempotent — absent token still returns ok."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        token = ''
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            token = (urllib.parse.parse_qs(qs).get('token') or [''])[0].strip()
        if not token:
            try:
                token = (self.read_json_body().get('token') or '').strip()
            except (json.JSONDecodeError, ValueError):
                token = ''
        if not token:
            self.send_json({'error': 'token is required'}, 400)
            return
        try:
            push_notify.PushTokenStore.unregister(token)
        except Exception as e:
            print(f'[push] unregister failed: {e}', file=sys.stderr)
            self.send_json({'error': 'could not remove token'}, 500)
            return
        self.send_json({'ok': True})


ROUTES = RouteTable()

#: Feed item ids carry an `fd_` prefix, which is what keeps the two per-item
#: routes from matching anything else under /api/feed/.
_ITEM = r'(fd_[A-Za-z0-9_]+)'

ROUTES.add('GET', '/api/feed', 'handle_feed_list')
ROUTES.add('GET', '/api/feed/unread_count', 'handle_feed_unread_count')

ROUTES.add('POST', '/api/feed', 'handle_feed_create')
ROUTES.add('POST', re.compile(rf'^/api/feed/{_ITEM}/read$'), 'handle_feed_read')
ROUTES.add('POST', re.compile(rf'^/api/feed/{_ITEM}/dismiss$'),
           'handle_feed_dismiss')

# Device-token registration for mobile push.
ROUTES.add('POST', '/api/push/register', 'handle_push_register')
ROUTES.add('POST', '/api/push/unregister', 'handle_push_unregister')
