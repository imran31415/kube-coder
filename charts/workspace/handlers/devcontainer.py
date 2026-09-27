"""devcontainer.json: read a repo's own dev-container config (#100, #594).

Four routes on two verbs.

`POST /api/devcontainer/apply` is the only route in the whole server that can
run a command out of a cloned repository, and it does so only against a
config hash the caller echoes back — so the gate is not incidental. Both it
and the parse-only reads go through `_devcontainer_gate`, which moves here
with them because nothing else calls it.
"""

import json
import re
import urllib.parse

import handlers
from handlers.routing import RouteTable


class DevcontainerRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def _devcontainer_gate(self):
        """(ok, workdir_or_empty). 401 unauthenticated, 404 when the feature is
        off. Deliberately NOT behind _require_cto: a deployment can run without
        the AI CTO and still want a repo's own environment read."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return False
        if not handlers.server.DevcontainerManager.available():
            self.send_json({'error': 'devcontainer support is disabled'}, 404)
            return False
        return True

    def _devcontainer_workdir(self, raw):
        workdir, err = handlers.server.DevcontainerManager.resolve_workdir(raw)
        if err:
            self.send_json({'error': err}, 400)
            return ''
        return workdir

    def _handle_devcontainer_get(self):
        """GET /api/devcontainer?workdir=<abs> — parsed config + what we did.

        A directory with no devcontainer.json answers 200 with found:false, not
        404. "There is no devcontainer here" is a normal, useful answer; a 404
        is indistinguishable from "the feature is disabled" and the SPA would
        have to guess which it got.
        """
        if not self._devcontainer_gate():
            return
        qs = urllib.parse.parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
        workdir = self._devcontainer_workdir((qs.get('workdir') or [''])[0])
        if not workdir:
            return
        try:
            self.send_json(handlers.server.DevcontainerManager.describe(workdir))
        except Exception as e:
            self.send_json({'error': f'read failed: {e}'}, 500)

    def _handle_devcontainer_scan(self):
        """GET /api/devcontainer/scan — every workspace dir that has one."""
        if not self._devcontainer_gate():
            return
        try:
            rows = handlers.server.DevcontainerManager.scan()
        except Exception as e:
            self.send_json({'error': f'scan failed: {e}'}, 500)
            return
        self.send_json({'devcontainers': rows, 'count': len(rows)})

    def _handle_devcontainer_apply(self):
        """POST /api/devcontainer/apply — the only executing route.

        Body: {workdir, hooks: [], config_hash, auto_apply}. `hooks` empty means
        appliers only (ports, settings, extensions, env) and nothing runs.
        `config_hash` is REQUIRED whenever hooks is non-empty and must equal the
        file's current hash — see DevcontainerManager.apply.
        """
        if not self._devcontainer_gate():
            return
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            body = json.loads(self.rfile.read(length) or b'{}') if length else {}
        except (ValueError, TypeError):
            self.send_json({'error': 'invalid JSON body'}, 400)
            return
        if not isinstance(body, dict):
            self.send_json({'error': 'body must be a JSON object'}, 400)
            return
        workdir = self._devcontainer_workdir(body.get('workdir'))
        if not workdir:
            return
        hooks = body.get('hooks') or []
        if not isinstance(hooks, list) or any(not isinstance(h, str) for h in hooks):
            self.send_json({'error': 'hooks must be an array of strings'}, 400)
            return
        unknown = [h for h in hooks if h not in handlers.server.devcontainer.HOOKS]
        if unknown:
            self.send_json({'error': f'unknown hooks: {", ".join(unknown)}',
                            'allowed': list(handlers.server.devcontainer.HOOKS)}, 400)
            return
        auto = body.get('auto_apply')
        result, err = handlers.server.DevcontainerManager.apply(
            workdir, hooks=hooks, config_hash=body.get('config_hash'),
            auto_apply=None if auto is None else bool(auto))
        if err:
            code, message = err
            status = {'not_found': 404, 'invalid': 422, 'hash_required': 400,
                      'hash_mismatch': 409, 'busy': 409}.get(code, 500)
            self.send_json({'error': message, 'code': code}, status)
            return
        # 202: appliers already ran synchronously, hooks (if any) are running in
        # a daemon thread and the caller follows them via devcontainer.changed.
        self.send_json(result, 202)

    def _handle_devcontainer_reset(self):
        """POST /api/devcontainer/reset — forget that we applied here."""
        if not self._devcontainer_gate():
            return
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            body = json.loads(self.rfile.read(length) or b'{}') if length else {}
        except (ValueError, TypeError):
            self.send_json({'error': 'invalid JSON body'}, 400)
            return
        if not isinstance(body, dict):
            self.send_json({'error': 'body must be a JSON object'}, 400)
            return
        workdir = self._devcontainer_workdir(body.get('workdir'))
        if not workdir:
            return
        try:
            self.send_json(handlers.server.DevcontainerManager.reset(
                workdir, unpin_ports=bool(body.get('unpin_ports'))))
        except Exception as e:
            self.send_json({'error': f'reset failed: {e}'}, 500)


ROUTES = RouteTable()

ROUTES.add('GET', '/api/devcontainer', '_handle_devcontainer_get')
ROUTES.add('GET', '/api/devcontainer/scan', '_handle_devcontainer_scan')
# The only route that can execute out of a cloned repo. See the docstring.
ROUTES.add('POST', '/api/devcontainer/apply', '_handle_devcontainer_apply')
ROUTES.add('POST', '/api/devcontainer/reset', '_handle_devcontainer_reset')
