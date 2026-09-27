"""Desktop launcher: the dashboard's pinned-app strip (#100).

Seven routes on three verbs, contiguous on each.

One shape is preserved rather than fixed: the per-item update is a POST to
`/api/desktop/{id}`, not a PUT. The chain routed it that way and the SPA
calls it that way, so the table does too — changing the verb would be an API
break dressed up as a refactor.
"""

import json
import re
import subprocess

import handlers
from handlers.routing import RouteTable


class DesktopRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def handle_desktop_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'items': handlers.server.DesktopManager.list_items()})

    def handle_desktop_get(self, item_id):
        """GET /api/desktop/{id} — one launcher item.

        The only handler in this series that did not exist before: do_GET
        carried this body inline in its dispatch branch rather than calling
        out to a method. Lifted verbatim so the table can name it; note it
        has no auth gate, exactly as the inline version had none.
        """
        item = handlers.server.DesktopManager.get(item_id)
        if item is None:
            self.send_json({'error': 'item not found'}, 404)
        else:
            self.send_json(item)

    def handle_desktop_create(self):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            item = handlers.server.DesktopManager.create(body)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json(item, 201)

    def handle_desktop_update(self, item_id):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            item = handlers.server.DesktopManager.update(item_id, body)
        except ValueError as e:
            code = 404 if 'not found' in str(e) else 400
            self.send_json({'error': str(e)}, code)
            return
        self.send_json(item)

    def handle_desktop_delete(self, item_id):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            handlers.server.DesktopManager.delete(item_id)
        except ValueError as e:
            code = 404 if 'not found' in str(e) else 400
            self.send_json({'error': str(e)}, code)
            return
        self.send_json({'ok': True})

    def handle_desktop_reorder(self):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        ids = body.get('order') if isinstance(body, dict) else None
        try:
            items = handlers.server.DesktopManager.reorder(ids or [])
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'items': items})

    def handle_desktop_launch(self, item_id):
        """Execute the icon's action server-side. `task` returns the
        created task_id; `shell` returns stdout/stderr/exit_code; `url`
        rejects (client opens the URL directly, server is just bookkeeping).
        Mutations gated by _readonly_block — viewing the launcher in the
        public demo is fine, firing it isn't."""
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        item = handlers.server.DesktopManager.get(item_id)
        if not item:
            self.send_json({'error': 'item not found'}, 404)
            return
        action = item.get('action', {})
        kind = action.get('type')
        if kind == 'task':
            task = handlers.server.ClaudeTaskManager.create_task(
                action.get('prompt', ''),
                workdir=action.get('workdir') or '/home/dev',
                source=f'desktop:{item_id}',
                assistant=action.get('assistant'),
            )
            if task.get('status') == 'rejected':
                self.send_json({'error': task.get('error')}, 429)
                return
            if task.get('status') == 'error':
                self.send_json({'error': task.get('error') or 'task spawn failed'}, 500)
                return
            self.send_json({'kind': 'task', 'task_id': task.get('task_id')}, 201)
        elif kind == 'shell':
            try:
                result = subprocess.run(
                    ['bash', '-lc', action.get('command', 'true')],
                    capture_output=True,
                    text=True,
                    timeout=int(action.get('timeout') or handlers.server.DesktopManager.SHELL_TIMEOUT_DEFAULT),
                    cwd='/home/dev',
                )
                self.send_json({
                    'kind': 'shell',
                    'exit_code': result.returncode,
                    'stdout': (result.stdout or '')[-8000:],
                    'stderr': (result.stderr or '')[-2000:],
                })
            except subprocess.TimeoutExpired:
                self.send_json({'error': 'command timed out', 'kind': 'shell'}, 504)
        elif kind == 'url':
            # The client opens URLs directly — no server work needed.
            # Return ok so the client can still report a launch event.
            self.send_json({'kind': 'url', 'url': action.get('url'), 'target': action.get('target', 'blank')})
        else:
            self.send_json({'error': f'unknown action type: {kind}'}, 400)


ROUTES = RouteTable()

#: Desktop item ids are lower-case alphanumeric.
_ITEM = r'([a-z0-9]+)'

ROUTES.add('GET', '/api/desktop', 'handle_desktop_list')
ROUTES.add('GET', re.compile(rf'^/api/desktop/{_ITEM}$'), 'handle_desktop_get')

ROUTES.add('POST', '/api/desktop', 'handle_desktop_create')
# `_reorder` has an underscore, which the item charset excludes, so it cannot
# be read as an item id.
ROUTES.add('POST', '/api/desktop/_reorder', 'handle_desktop_reorder')
ROUTES.add('POST', re.compile(rf'^/api/desktop/{_ITEM}/launch$'),
           'handle_desktop_launch')
# POST, not PUT — see the module docstring.
ROUTES.add('POST', re.compile(rf'^/api/desktop/{_ITEM}$'),
           'handle_desktop_update')

ROUTES.add('DELETE', re.compile(rf'^/api/desktop/{_ITEM}$'),
           'handle_desktop_delete')
