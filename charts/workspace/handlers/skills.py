"""Multi-harness SKILL.md surface: list / detail / stats / scan / sync (#100).

The Skills tab's API (issue #187). Reads come from the SkillsSyncer's
in-memory snapshot — files are the source of truth — and the two writes force
a rescan and install one skill into another harness's native directory.

The first domain in this series with routes on two verbs, so one table carries
both: `http_method` is part of the match, and do_GET and do_POST each consult
`ROUTES` at the point their own branches used to sit.

Ordering notes now carried as table data:

  * `/api/skills/stats` has to precede the `^/api/skills/(<name>)$` detail
    route, which would otherwise read `stats` as a skill name.
  * The two POST routes were 70 lines apart in do_POST — `_scan` in the exact
    if/elif chain, `{name}/sync` down in the regex block below it. Neither
    pattern matches the other (`_` is outside the sync route's charset) and no
    route registered between them in the old chain matches a `/api/skills/…`
    path, so the table's adjacency is the same dispatch the chain performed.
  * The GET detail route and the POST sync route take DIFFERENT name charsets,
    on purpose: only the filesystem-safe `[a-z0-9-]` set may ever build a
    write path. Preserved verbatim.

`handle_skills_list` reads the request's query string, which do_GET used to
parse inline and hand over as an argument; that is the route's `query=True`
column — see handlers/routing.py.
"""

import json
import re
import sys

import handlers
from handlers.routing import RouteTable


class SkillsRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def _skills_unavailable(self):
        if handlers.server._SKILLS_AVAILABLE:
            return False
        self.send_json({'error': 'skills subsystem unavailable',
                        'code': 'skills_unavailable',
                        'detail': 'skills package failed to import; check server logs'},
                       503)
        return True

    def handle_skills_list(self, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        if (query.get('refresh') or [''])[0] in ('1', 'true'):
            try:
                handlers.server.SkillsSyncer.trigger_sync()
            except Exception as e:
                print(f'[skills] refresh failed: {e}', file=sys.stderr)
        records = handlers.server.SkillsSyncer.snapshot()
        system = (query.get('system') or [None])[0]
        scope = (query.get('scope') or [None])[0]
        if system:
            records = [r for r in records if system in r.systems]
        if scope:
            records = [r for r in records if r.scope == scope]
        self.send_json({'skills': [r.to_dict() for r in records],
                        'count': len(records)})

    def handle_skills_get(self, name):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        variants = handlers.server.SkillsSyncer.get(name)
        if not variants:
            self.send_json({'error': 'not found', 'code': 'not_found'}, 404)
            return
        # One logical skill normally; 2+ entries when copies have diverged
        # across systems (same name, different content fingerprint).
        self.send_json({'skill': variants[0].to_dict(),
                        'variants': [v.to_dict() for v in variants],
                        'divergent': len(variants) > 1})

    def handle_skills_stats(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        records = handlers.server.SkillsSyncer.snapshot()
        by_system, by_scope = {}, {}
        for r in records:
            for s in r.systems:
                by_system[s] = by_system.get(s, 0) + 1
            by_scope[r.scope] = by_scope.get(r.scope, 0) + 1
        self.send_json({'total': len(records),
                        'by_system': by_system,
                        'by_scope': by_scope,
                        'syncer': handlers.server.SkillsSyncer.status()})

    def handle_skills_scan(self):
        """POST /api/skills/_scan — synchronous forced rescan."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        try:
            res = handlers.server.SkillsSyncer.trigger_sync()
        except Exception as e:
            self.send_json({'error': f'scan failed: {e}'}, 500)
            return
        self.send_json({'status': 'ok', 'result': res})

    def handle_skills_sync(self, name):
        """POST /api/skills/{name}/sync — cross-harness install (PR2).

        Body: {source_system, source_scope?, targets:[{system, scope?}],
        force?}. Translates one logical skill's source variant into each
        target harness's native dir so every agent can use it. Any-to-any:
        source_system is a parameter, not fixed to Claude.

        Guarded: readonly (do_POST chokepoint), name charset, unknown/
        disabled targets (400), and 409 when a target already holds a
        DIVERGENT copy (different fingerprint) unless {"force": true} —
        so a sync never silently clobbers a locally-edited skill.
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        # Belt-and-suspenders: the route regex already restricts the charset,
        # but re-check against the canonical gate before any path is built.
        if not (handlers.server.SKILL_NAME_RE and handlers.server.SKILL_NAME_RE.match(name)):
            self.send_json({'error': 'invalid skill name', 'code': 'bad_name'}, 400)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return

        source_system = (data.get('source_system') or '').strip()
        source_scope = (data.get('source_scope') or '').strip() or None
        force = bool(data.get('force'))
        targets = data.get('targets')
        if not isinstance(targets, list) or not targets:
            self.send_json({'error': 'targets[] required'}, 400)
            return

        # --- resolve the SOURCE variant from the live snapshot ---
        variants = handlers.server.SkillsSyncer.get(name)
        if not variants:
            self.send_json({'error': 'not found', 'code': 'not_found'}, 404)
            return
        candidates = variants
        if source_system:
            candidates = [v for v in candidates if source_system in v.systems]
        if source_scope:
            candidates = [v for v in candidates if v.scope == source_scope]
        if not candidates:
            self.send_json({'error': 'source variant not found',
                            'code': 'source_not_found'}, 404)
            return
        if len(candidates) > 1:
            # Divergent skill and the caller didn't pin it down.
            self.send_json({'error': 'ambiguous source; specify source_system',
                            'code': 'ambiguous_source'}, 400)
            return
        source = candidates[0]

        # --- validate every target up front (fail fast, install nothing) ---
        norm_targets = []
        for t in targets:
            if not isinstance(t, dict):
                self.send_json({'error': 'each target must be an object'}, 400)
                return
            sys_key = (t.get('system') or '').strip()
            scope = (t.get('scope') or 'user').strip()
            provider = handlers.server.SKILL_PROVIDERS.get(sys_key)
            if provider is None:
                self.send_json({'error': f'unknown target system: {sys_key!r}',
                                'code': 'bad_target'}, 400)
                return
            if not provider.enabled:
                self.send_json({'error': f'target {sys_key!r} is disabled',
                                'code': 'target_disabled'}, 400)
                return
            try:
                provider.install_path(name, scope)  # validates scope writable
            except ValueError as e:
                self.send_json({'error': str(e), 'code': 'bad_target'}, 400)
                return
            norm_targets.append((sys_key, scope, provider))

        # --- conflict check: refuse to overwrite a DIVERGENT target copy ---
        conflicts = []
        for sys_key, scope, _provider in norm_targets:
            existing = [v for v in variants if sys_key in v.systems]
            for ex in existing:
                if ex.fingerprint != source.fingerprint:
                    conflicts.append({'system': sys_key,
                                      'existing_fingerprint': ex.fingerprint})
                    break
        if conflicts and not force:
            self.send_json({'error': 'target has a divergent copy',
                            'code': 'conflict', 'conflicts': conflicts,
                            'hint': 'retry with {"force": true} to overwrite'},
                           409)
            return

        # --- install into every target (atomic per file) ---
        installed, failed = [], []
        for sys_key, scope, provider in norm_targets:
            try:
                path = provider.install(source, scope)
                installed.append({'system': sys_key, 'scope': scope, 'path': path})
            except Exception as e:
                failed.append({'system': sys_key, 'scope': scope, 'error': str(e)})
        # Refresh the snapshot so the collapsed row is visible immediately
        # and clients get a fresh list on their next poll / SSE tick.
        try:
            handlers.server.SkillsSyncer.trigger_sync()
        except Exception:
            pass
        status = 200 if installed and not failed else (207 if installed else 500)
        self.send_json({'name': name, 'source_system': source.systems[0],
                        'installed': installed, 'failed': failed}, status)


#: Consulted by do_GET and do_POST where each verb's branches used to sit.
#: Registration order is match order.
ROUTES = RouteTable()

ROUTES.add('GET', '/api/skills', 'handle_skills_list', query=True)
# ORDERING HAZARD: `stats` also matches the detail pattern below.
ROUTES.add('GET', '/api/skills/stats', 'handle_skills_stats')
ROUTES.add('GET', re.compile(r'^/api/skills/([a-zA-Z0-9._-]+)$'), 'handle_skills_get')

ROUTES.add('POST', '/api/skills/_scan', 'handle_skills_scan')
# Stricter charset than the GET route above — see the module docstring.
ROUTES.add('POST', re.compile(r'^/api/skills/([a-z0-9-]+)/sync$'), 'handle_skills_sync')
