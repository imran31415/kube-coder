"""Project registry and the AI CTO brief (#100, #464).

Seven routes across four verbs, and the thinnest domain in the series apart
from docs. Contiguous on every verb, so the table is a straight lift.

`_require_cto` moves with them: every handler here calls it and nothing else
does. It is the gate that makes the whole surface disappear when the AI CTO
is not enabled — a 404, not a 403, so a deployment without it looks like a
deployment that never had the routes.
"""

import json
import re

import handlers
from handlers.routing import RouteTable


class ProjectRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def _require_cto(self):
        """Gate the AI CTO API behind cto_available() (#467). Sends a 404 and
        returns False when the feature is off, so a disabled deployment exposes
        no projects surface."""
        if handlers.server.cto_available():
            return True
        self.send_json({'error': 'AI CTO is disabled'}, 404)
        return False

    def handle_project_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not self._require_cto():
            return
        self.send_json({'projects': handlers.server.ProjectsManager.list_projects()})

    def handle_project_get(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not self._require_cto():
            return
        cfg = handlers.server.ProjectsManager.get_project(self._project_id)
        if cfg is None:
            self.send_json({'error': 'Project not found'}, 404)
            return
        self.send_json(cfg)

    def handle_project_brief(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not self._require_cto():
            return
        brief = handlers.server.ProjectsManager.brief(self._project_id)
        if brief is None:
            self.send_json({'error': 'Project not found'}, 404)
            return
        self.send_json(brief)

    def handle_project_create(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not self._require_cto():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = handlers.server.ProjectsManager.create(data)
        if err:
            self.send_json({'error': err},
                           409 if 'already exists' in err else 400)
            return
        handlers.server.EventBroker.publish('projects.changed', {'op': 'create', 'id': cfg['id']})
        self.send_json(cfg, 201)

    def handle_project_update(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not self._require_cto():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = handlers.server.ProjectsManager.update(self._project_id, data)
        if err:
            self.send_json({'error': err},
                           404 if err == 'not found' else 400)
            return
        handlers.server.EventBroker.publish('projects.changed', {'op': 'update', 'id': cfg['id']})
        self.send_json(cfg)

    def handle_project_delete(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not self._require_cto():
            return
        if not handlers.server.ProjectsManager.delete(self._project_id):
            self.send_json({'error': 'Project not found'}, 404)
            return
        handlers.server.EventBroker.publish('projects.changed', {'op': 'delete', 'id': self._project_id})
        self.send_json({'ok': True})

    def handle_project_discover(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not self._require_cto():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            data = {}
        result = handlers.server.ProjectsManager.discover(
            auto_provision=bool(data.get('auto_provision', True)))
        for pid in result.get('registered', []):
            handlers.server.EventBroker.publish('projects.changed', {'op': 'create', 'id': pid})
        self.send_json(result)


ROUTES = RouteTable()

#: Project ids are generated slugs.
_PROJECT = r'([a-z0-9-]+)'

ROUTES.add('GET', '/api/projects', 'handle_project_list')
# Sub-resource before the bare {id}; anchored, so presentational.
ROUTES.add('GET', re.compile(rf'^/api/projects/{_PROJECT}/brief$'),
           'handle_project_brief', sets='_project_id')
ROUTES.add('GET', re.compile(rf'^/api/projects/{_PROJECT}$'),
           'handle_project_get', sets='_project_id')

ROUTES.add('POST', '/api/projects', 'handle_project_create')
# `_discover` is not a legal project id (underscore), so it cannot collide.
ROUTES.add('POST', '/api/projects/_discover', 'handle_project_discover')

ROUTES.add('PUT', re.compile(rf'^/api/projects/{_PROJECT}$'),
           'handle_project_update', sets='_project_id')
ROUTES.add('DELETE', re.compile(rf'^/api/projects/{_PROJECT}$'),
           'handle_project_delete', sets='_project_id')
