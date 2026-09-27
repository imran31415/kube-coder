"""Memory API: the dashboard's Memory tab, mirroring the MCP tools (#100).

Sixteen routes across three verbs — the largest domain migrated so far, and
the one #733's inventory flagged for its `{ns}/{key}` sub-resource family.

## About that ordering hazard

It is **presentational, not load-bearing**, and the tests below say so rather
than implying otherwise. A namespace or key is `[a-zA-Z0-9._-]+`, which
excludes `/`, and every pattern is `$`-anchored — so `{ns}/{key}` cannot
swallow `{ns}/{key}/history` no matter where it sits, and `stats` / `export`
are one segment where `{ns}/{key}` needs two. Registration still follows the
chain's order, because the day someone widens a charset to include `/` the
order is what saves them.

## Two adapters, and why they exist rather than edits

The moved bodies are byte-identical apart from the `handlers.server.` prefix,
which leaves two dispatch-site conversions with nowhere to live. Rather than
change a handler's signature or its first line, each gets a one-line adapter
named by the route — the conversion stays visible, and the handler a reviewer
diffs against the old file is untouched:

  * `route_memory_unlink` — the table passes capture groups as strings and
    `unlink_by_id` matches an INTEGER primary key.
  * `route_memory_neighbors` — `query=True` hands the parsed query string to
    the handler first, and this is the one handler that takes it last.

## The last of `memory_query`

`do_GET` parsed the query string inline for exactly two handlers in this
domain (`handle_memory_list`, `handle_memory_neighbors`); PR 2 turned that
into the table's `query=` column, and with this domain gone the two locals
leave `do_GET` with it.
"""

import json
import re
import sys

import handlers
from handlers.routing import RouteTable


class MemoryRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    # --- Memory API handlers ---------------------------------------------
    # The dashboard's Memory tab consumes these endpoints. They mirror the
    # MCP tool surface (mcp_memory.py) so the dashboard and Claude share a
    # single SQLite store with consistent semantics.

    def _memory_unavailable(self):
        if handlers.server._MEMORY_AVAILABLE:
            return False
        self.send_json({
            'error': 'memory subsystem unavailable',
            'detail': 'memory.manager failed to import; check server logs',
        }, 503)
        return True

    def _memory_error(self, e):
        if isinstance(e, handlers.server.MemNotFound):
            self.send_json({'error': str(e), 'code': 'not_found'}, 404)
            return
        if isinstance(e, handlers.server.MemConflict):
            self.send_json({'error': str(e), 'code': 'conflict'}, 409)
            return
        if isinstance(e, handlers.server.MemValidationError):
            self.send_json({'error': str(e), 'code': 'validation'}, 400)
            return
        if isinstance(e, handlers.server.MemError):
            self.send_json({'error': str(e), 'code': e.code}, 400)
            return
        print(f'[memory] internal error: {e}', file=sys.stderr)
        self.send_json({'error': str(e), 'code': 'internal'}, 500)

    def handle_memory_list(self, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = handlers.server.MemoryManager.list(
                namespace=(query.get('namespace') or [None])[0],
                kind=(query.get('kind') or [None])[0],
                q=(query.get('q') or [None])[0],
                limit=int((query.get('limit') or ['500'])[0]),
                # Namespace-scoped retrieval (#359): confines results to one
                # namespace root and everything nested under it, so a
                # project-bound caller (the inject hook) never sees a sibling
                # project's memories — plus `user.*`, which a scoped read always
                # includes (#593). Absent => workspace-global, as before.
                namespace_scope=(query.get('namespace_scope') or [None])[0],
            )
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'memories': rows, 'count': len(rows)})

    def handle_memory_get(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            row = handlers.server.MemoryManager.get(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        if row is None:
            self.send_json({'error': 'not found', 'code': 'not_found'}, 404)
            return
        # Log read access.
        try:
            actor = self._memory_actor()
            kind = actor.split(':', 1)[0]
            ident = actor.split(':', 1)[1] if ':' in actor else actor
            handlers.server.MemoryManager.log_ref(
                namespace=namespace, key=key,
                ref_kind=kind if kind in ('dashboard', 'api', 'cron', 'task') else 'api',
                ref_id=ident, access_kind='read',
            )
        except Exception:
            pass
        self.send_json({'memory': row})

    def handle_memory_history(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = handlers.server.MemoryManager.history(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'revisions': rows, 'count': len(rows)})

    def handle_memory_refs(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = handlers.server.MemoryManager.refs(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'refs': rows, 'count': len(rows)})

    def handle_memory_relations(self, namespace, key):
        """GET /api/memory/{ns}/{key}/relations — the memory's graph edges with
        ids, so the dashboard can render + unlink them (#134)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = handlers.server.MemoryManager.relations(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'relations': rows, 'count': len(rows)})

    def handle_memory_neighbors(self, namespace, key, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        depth = int((query.get('depth') or ['1'])[0])
        kinds = query.get('kind') or query.get('kinds') or None
        try:
            rows = handlers.server.MemoryManager.neighbors(
                namespace=namespace, key=key, depth=depth,
                kinds=kinds,
            )
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'neighbors': rows, 'count': len(rows)})

    def handle_memory_stats(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            stats = handlers.server.MemoryManager.stats()
            # Surface syncer status so the dashboard can show "last
            # imported N from Claude's auto-memory · X minutes ago".
            try:
                stats['claude_sync'] = handlers.server.ClaudeMemorySyncer.status()
            except Exception:
                pass
            # Surface embedding-worker status so the Memory tab can show the
            # semantic-search backlog draining (or that it's disabled).
            try:
                stats['embedding_worker'] = handlers.server.EmbeddingWorker.status()
            except Exception:
                pass
            self.send_json(stats)
        except Exception as e:
            self._memory_error(e)

    def handle_memory_sync_claude(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            res = handlers.server.ClaudeMemorySyncer.trigger_sync()
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'status': 'ok', 'result': res})

    def handle_memory_upsert(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return
        try:
            row = handlers.server.MemoryManager.upsert(
                namespace=data.get('namespace', ''),
                key=data.get('key', ''),
                value=data.get('value', ''),
                kind=data.get('kind', 'semantic'),
                tags=data.get('tags', '') or '',
                importance=float(data.get('importance', 0.5)),
                confidence=float(data.get('confidence', 1.0)),
                source=self._memory_actor(),
                expires_at=data.get('expires_at'),
            )
        except Exception as e:
            self._memory_error(e); return
        try:
            actor = self._memory_actor()
            kind = actor.split(':', 1)[0]
            ident = actor.split(':', 1)[1] if ':' in actor else actor
            handlers.server.MemoryManager.log_ref(
                namespace=row['namespace'], key=row['key'],
                ref_kind=kind if kind in ('dashboard', 'api') else 'api',
                ref_id=ident, access_kind='write',
            )
        except Exception:
            pass
        handlers.server.EventBroker.publish('memory.changed', {
            'op': 'upsert', 'namespace': row['namespace'], 'key': row['key'],
        })
        # Feed (#469): a recorded decision is feed-worthy. tags_list is on the
        # row; fall back to splitting the raw tags string.
        tags = row.get('tags_list')
        if tags is None:
            tags = [t.strip() for t in (row.get('tags') or '').split(',') if t.strip()]
        if 'decision' in tags:
            handlers.server.FeedManager.emit_decision(row['namespace'], row['key'], row.get('value') or '')
        self.send_json({'memory': row}, 200)

    def handle_memory_delete(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            row = handlers.server.MemoryManager.soft_delete(
                namespace=namespace, key=key,
                source=self._memory_actor(),
            )
        except Exception as e:
            self._memory_error(e); return
        handlers.server.EventBroker.publish('memory.changed', {
            'op': 'delete', 'namespace': namespace, 'key': key,
        })
        self.send_json({'memory': row, 'deleted': True})

    def handle_memory_link(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return
        try:
            rel = handlers.server.MemoryManager.link(
                src_namespace=namespace, src_key=key,
                dst_namespace=data.get('dst_namespace', ''),
                dst_key=data.get('dst_key', ''),
                kind=data.get('kind', 'related-to'),
                weight=float(data.get('weight', 1.0)),
                created_by=self._memory_actor(),
            )
        except Exception as e:
            self._memory_error(e); return
        handlers.server.EventBroker.publish('memory.changed', {
            'op': 'link', 'namespace': namespace, 'key': key,
        })
        self.send_json({'relation': rel}, 201)

    def handle_memory_unlink(self, namespace, key, relation_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            deleted = handlers.server.MemoryManager.unlink_by_id(
                relation_id=relation_id, namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        if deleted:
            handlers.server.EventBroker.publish('memory.changed', {
                'op': 'unlink', 'namespace': namespace, 'key': key,
            })
        self.send_json({'deleted': deleted}, 200 if deleted else 404)

    def handle_memory_consolidate(self):
        """Phase 1 stub. Returns 202 with a no-op so the dashboard button can
        be wired ahead of Phase 3 landing the real worker."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        self.send_json({
            'status': 'queued',
            'detail': 'consolidation worker activates in Phase 3',
        }, 202)

    def handle_memory_export(self):
        """GET /api/memory/export — JSON dump of live memories + relations,
        suitable for backup or moving a corpus between workspaces (#107)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            payload = handlers.server.MemoryManager.export_json()
        except Exception as e:
            self._memory_error(e); return
        self.send_json(payload)

    def handle_memory_import(self):
        """POST /api/memory/_import — load a corpus produced by export. Body:
        {memories:[...], relations:[...], mode?:'merge'|'skip'}. Mutating, so
        the do_POST readonly chokepoint already gates it (#107)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return
        try:
            res = handlers.server.MemoryManager.import_json(
                data, mode=data.get('mode', 'merge'),
                source=self._memory_actor())
        except Exception as e:
            self._memory_error(e); return
        handlers.server.EventBroker.publish('memory.changed', {'op': 'import'})
        self.send_json({'status': 'ok', 'result': res})

    def handle_memory_purge(self):
        """POST /api/memory/_purge — hard-delete soft-deleted memories and
        VACUUM. Body: {older_than_days?: number}. Mutating; readonly-gated by
        the do_POST chokepoint (#107)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        older = None
        try:
            data = self.read_json_body()  # {} when body is empty
            if isinstance(data, dict) and data.get('older_than_days') is not None:
                older = float(data['older_than_days'])
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            res = handlers.server.MemoryManager.purge_deleted(older_than_days=older)
        except Exception as e:
            self._memory_error(e); return
        handlers.server.EventBroker.publish('memory.changed', {'op': 'purge'})
        self.send_json({'status': 'ok', 'result': res})

    # --- Adapters: the two dispatch-site conversions (see module docstring) --

    def route_memory_neighbors(self, query, namespace, key):
        """`query=True` puts the parsed query string first; this handler
        takes it last."""
        self.handle_memory_neighbors(namespace, key, query)

    def route_memory_unlink(self, namespace, key, relation_id):
        """Capture groups arrive as strings; `unlink_by_id` matches an
        INTEGER primary key, so a string would never match a row."""
        self.handle_memory_unlink(namespace, key, int(relation_id))


#: Consulted by do_GET, do_POST and do_DELETE where each verb's branches used
#: to sit. Registration order is the chain's order.
ROUTES = RouteTable()

#: A namespace or a key. No '/', which is what makes the sub-resource routes
#: below unable to collide with the bare `{ns}/{key}` one.
_SEG = r'([a-zA-Z0-9._-]+)'

ROUTES.add('GET', '/api/memory', 'handle_memory_list', query=True)
ROUTES.add('GET', '/api/memory/stats', 'handle_memory_stats')
ROUTES.add('GET', '/api/memory/export', 'handle_memory_export')
# The sub-resource family, ahead of the bare `{ns}/{key}` read. Anchored and
# slash-free, so today this order is documentation rather than dispatch.
ROUTES.add('GET', re.compile(rf'^/api/memory/{_SEG}/{_SEG}/history$'),
           'handle_memory_history')
ROUTES.add('GET', re.compile(rf'^/api/memory/{_SEG}/{_SEG}/refs$'),
           'handle_memory_refs')
ROUTES.add('GET', re.compile(rf'^/api/memory/{_SEG}/{_SEG}/relations$'),
           'handle_memory_relations')
ROUTES.add('GET', re.compile(rf'^/api/memory/{_SEG}/{_SEG}/neighbors$'),
           'route_memory_neighbors', query=True)
ROUTES.add('GET', re.compile(rf'^/api/memory/{_SEG}/{_SEG}$'),
           'handle_memory_get')

ROUTES.add('POST', '/api/memory', 'handle_memory_upsert')
ROUTES.add('POST', '/api/memory/_consolidate', 'handle_memory_consolidate')
ROUTES.add('POST', '/api/memory/_sync_claude', 'handle_memory_sync_claude')
ROUTES.add('POST', '/api/memory/_import', 'handle_memory_import')
ROUTES.add('POST', '/api/memory/_purge', 'handle_memory_purge')
# Was in do_POST's regex block, ~160 lines below the five exact routes above.
ROUTES.add('POST', re.compile(rf'^/api/memory/{_SEG}/{_SEG}/relations$'),
           'handle_memory_link')

ROUTES.add('DELETE', re.compile(rf'^/api/memory/{_SEG}/{_SEG}/relations/(\d+)$'),
           'route_memory_unlink')
ROUTES.add('DELETE', re.compile(rf'^/api/memory/{_SEG}/{_SEG}$'),
           'handle_memory_delete')
