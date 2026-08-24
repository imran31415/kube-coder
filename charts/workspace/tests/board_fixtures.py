"""Worked connector configs for the three boards Phase 1 must prove.

Not a test module (no `_test` suffix, so unittest discovery skips it) — these
are shared fixtures AND the honest answer to the question the epic says is the
real risk: can ONE declarative schema express boards that disagree about
everything?

Between them they cover every hard axis:

CAVEAT: these are ENGINE fixtures, not connectors anyone should copy into
production. They exercise schema and engine behaviour against a stubbed HTTP
layer, so vendor quirks that only a live API enforces are deliberately not
reproduced here — the Jira comment body below is a plain string, which real
Jira v3 refuses (it requires ADF), and the Jira list request omits the `fields`
parameter, without which real Jira returns only `id`. The SHIPPED starter
connectors live in `boards/templates.py` and are correct on both counts; copy
from there.

  GitHub Issues — REST, Link-header pagination, a top-level array response, the
                  brokered workspace credential, and the state+state_reason
                  composite that must round-trip.
  Jira          — multi-step transition with `select` (transition ids are not
                  stable), opaque page-token pagination, and no total.
  Linear        — GraphQL: the URL does not encode the operation, the query
                  lives in the request BODY, and the cursor is injected into
                  that body rather than the query string.
"""

# ── GitHub Issues ──────────────────────────────────────────────────────────
# Secondary rate limits arrive as 200 or 403 rather than 429, so limit_detect
# lists those statuses AND requires a body phrase — otherwise an ordinary
# permission 403 would be retried forever as if it were throttling.
GITHUB = {
    'vendor': 'github',
    'display_name': 'kube-coder — issues',
    'base_url': 'https://api.github.com',
    'credential_ref': '@workspace-github',
    'auth': {'kind': 'bearer'},
    'list': {
        'request': {
            'method': 'GET',
            'url': '${base_url}/repos/imran31415/kube-coder/issues',
            'query': {'state': 'all', 'per_page': '50'},
            'headers': {'Accept': 'application/vnd.github+json'},
        },
        'items_path': '',            # the response body IS the array
        'page_size': 50,
        'max_pages': 20,
        'pagination': {'kind': 'link_header', 'rel': 'next'},
    },
    'map': {
        'id': 'id',                  # global id — markers key on this
        'key': 'number',             # per-repo number — what humans quote
        'ref': {'owner': {'template': 'imran31415'},
                'repo': {'template': 'kube-coder'},
                'number': 'number'},
        'title': 'title',
        'body': 'body',
        # closed+completed and closed+not_planned both normalize to CLOSED and
        # differ only in raw. For a board processor that difference matters.
        'status_raw': {'join': ['state', 'state_reason'], 'sep': '+'},
        'assignee': {'id': 'assignee.id', 'name': 'assignee.login'},
        'contact': {'id': 'user.id', 'name': 'user.login'},
        'collection': {'id': 'repository_url', 'name': 'repository_url'},
        'tags': 'labels',
        'url': 'html_url',
        'created_at': 'created_at',
        'updated_at': 'updated_at',
    },
    'enums': {
        'status': {
            'OPEN': ['open'],
            'CLOSED': ['closed', 'closed+completed', 'closed+not_planned'],
        },
    },
    'actions': {
        'comment': {
            'params': {'body': {'type': 'string', 'required': True}},
            'writes': 1,
            'idempotency': {
                'marker_template': '<!-- kc:${board_id}:${item.id}:${action_hash} -->',
                'probe': {
                    'method': 'GET',
                    'url': '${base_url}/repos/${item.ref.owner}/${item.ref.repo}'
                           '/issues/${item.ref.number}/comments',
                },
                'probe_items_path': '',
                'probe_field': 'body',
            },
            'steps': [{
                'method': 'POST',
                'url': '${base_url}/repos/${item.ref.owner}/${item.ref.repo}'
                       '/issues/${item.ref.number}/comments',
                'body': {'body': '${params.body}\n\n${marker}'},
            }],
        },
        'set_status': {
            'params': {'state': {'type': 'string', 'required': True},
                       'state_reason': {'type': 'string'}},
            'writes': 1,
            'steps': [{
                'method': 'PATCH',
                'url': '${base_url}/repos/${item.ref.owner}/${item.ref.repo}'
                       '/issues/${item.ref.number}',
                'body': {'state': '${params.state}'},
            }],
        },
    },
    'limits': {
        'global': {'max_events': 900, 'window_seconds': 60},
        'per_item_writes': 10,
        'limit_detect': {
            'statuses': [403, 429],
            'body_contains': ['secondary rate limit', 'api rate limit exceeded'],
        },
    },
}


# ── Jira ───────────────────────────────────────────────────────────────────
# /search/jql returns an opaque nextPageToken and NO total, and silently drops
# the remainder of an over-large result set. The engine's full-page-with-no-
# token rule is what turns that from silent truncation into complete=False.
JIRA = {
    'vendor': 'jira',
    'display_name': 'Acme Jira — Support',
    'base_url': 'https://acme.atlassian.net',
    'credential_ref': '@board-creds/JIRA_API_TOKEN',
    'auth': {'kind': 'header', 'header': 'Authorization',
             'template': 'Basic ${credential}'},
    'list': {
        'request': {
            'method': 'GET',
            'url': '${base_url}/rest/api/3/search/jql',
            'query': {'jql': 'project = SUP ORDER BY updated DESC',
                      'maxResults': '50'},
            'headers': {'Accept': 'application/json'},
        },
        'items_path': 'issues',
        'page_size': 50,
        'max_pages': 20,
        'pagination': {'kind': 'page_token', 'token_path': 'nextPageToken',
                       'into': {'query': 'nextPageToken'}},
    },
    'map': {
        'id': 'id',
        'key': 'key',
        'ref': {'issue_key': 'key'},
        'title': 'fields.summary',
        'body': 'fields.description',
        'status_raw': 'fields.status.name',
        'priority_raw': 'fields.priority.name',
        'assignee': {'id': 'fields.assignee.accountId',
                     'name': 'fields.assignee.displayName',
                     'email': 'fields.assignee.emailAddress'},
        'contact': {'id': 'fields.reporter.accountId',
                    'name': 'fields.reporter.displayName',
                    'email': 'fields.reporter.emailAddress'},
        'collection': {'id': 'fields.project.id', 'name': 'fields.project.name'},
        'tags': 'fields.labels',
        'url': {'template': '${base_url}/browse/${item.key}'},
        'created_at': 'fields.created',
        'updated_at': 'fields.updated',
    },
    'enums': {
        'status': {'OPEN': ['To Do', 'Open', 'Backlog'],
                   'IN_PROGRESS': ['In Progress', 'In Review'],
                   'ON_HOLD': ['Blocked'],
                   'CLOSED': ['Done', 'Closed']},
        'priority': {'URGENT': ['Highest', 'P1'], 'HIGH': ['High', 'P2'],
                     'NORMAL': ['Medium', 'P3'], 'LOW': ['Low', 'Lowest', 'P4']},
    },
    'actions': {
        # The transition set is status- AND permission-dependent, so ids are
        # not stable: they must be looked up per item, per call. A one-shot
        # {method,url,body} cannot express this, which is why steps + select
        # exist at all.
        'set_status': {
            'params': {'status': {'type': 'string', 'required': True}},
            'writes': 1,
            'steps': [
                {'id': 't', 'method': 'GET',
                 'url': '${base_url}/rest/api/3/issue/${item.ref.issue_key}'
                        '/transitions?expand=transitions.fields'},
                {'method': 'POST',
                 'url': '${base_url}/rest/api/3/issue/${item.ref.issue_key}'
                        '/transitions',
                 'select': {'from': 't.transitions',
                            'where': {'to.name': '${params.status}'},
                            'as': 'tr'},
                 'body': {'transition': {'id': '${tr.id}'}}},
            ],
        },
        'comment': {
            'params': {'body': {'type': 'string', 'required': True}},
            'writes': 1,
            'idempotency': {
                'marker_template': '[kc:${board_id}:${item.id}:${action_hash}]',
                'probe': {'method': 'GET',
                          'url': '${base_url}/rest/api/3/issue/'
                                 '${item.ref.issue_key}/comment'},
                'probe_items_path': 'comments',
                'probe_field': 'body',
            },
            'steps': [{
                'method': 'POST',
                'url': '${base_url}/rest/api/3/issue/${item.ref.issue_key}/comment',
                'body': {'body': '${params.body}\n\n${marker}'},
            }],
        },
    },
    'limits': {
        'global': {'max_events': 100, 'window_seconds': 60},
        'per_action': {'comment': {'max_events': 30, 'window_seconds': 600}},
        'per_item_writes': 8,
    },
}


# ── Linear (GraphQL) ───────────────────────────────────────────────────────
# The URL is the same for every operation; the operation is the BODY. If the
# schema assumed the URL encoded the operation, Monday, Linear and GitHub
# Projects v2 would all be excluded outright.
LINEAR_QUERY = (
    'query Issues($after: String) { issues(first: 50, after: $after) { '
    'nodes { id identifier title description updatedAt createdAt url '
    'state { name type } priority labels { nodes { name } } '
    'assignee { id name email } } '
    'pageInfo { hasNextPage endCursor } } }'
)

LINEAR = {
    'vendor': 'linear',
    'display_name': 'Linear — Engineering',
    'base_url': 'https://api.linear.app',
    'credential_ref': '@board-creds/LINEAR_API_KEY',
    'auth': {'kind': 'header', 'header': 'Authorization',
             'template': '${credential}'},
    'list': {
        'request': {
            'method': 'POST',
            'url': '${base_url}/graphql',
            'headers': {'Content-Type': 'application/json'},
            'body': {'query': LINEAR_QUERY, 'variables': {'after': None}},
        },
        'items_path': 'data.issues.nodes',
        'page_size': 50,
        'max_pages': 20,
        # The cursor goes into the BODY, not the query string — and it expires,
        # so a run can never store one, pause, and resume.
        'pagination': {'kind': 'cursor',
                       'cursor_path': 'data.issues.pageInfo.endCursor',
                       'has_more_path': 'data.issues.pageInfo.hasNextPage',
                       'into': {'body': 'variables.after'}},
    },
    'map': {
        'id': 'id',
        'key': 'identifier',
        'ref': {'id': 'id'},
        'title': 'title',
        'body': 'description',
        'status_raw': 'state.name',
        'priority_raw': 'priority',
        'assignee': {'id': 'assignee.id', 'name': 'assignee.name',
                     'email': 'assignee.email'},
        'url': 'url',
        'created_at': 'createdAt',
        'updated_at': 'updatedAt',
    },
    'enums': {
        'status': {'OPEN': ['Backlog', 'Todo'], 'IN_PROGRESS': ['In Progress'],
                   'ON_HOLD': ['Blocked'], 'CLOSED': ['Done', 'Canceled']},
        'priority': {'URGENT': ['1'], 'HIGH': ['2'], 'NORMAL': ['3'],
                     'LOW': ['4']},
    },
    'actions': {
        'comment': {
            'params': {'body': {'type': 'string', 'required': True}},
            'writes': 1,
            'steps': [{
                'method': 'POST',
                'url': '${base_url}/graphql',
                'body': {
                    'query': 'mutation($id: String!, $body: String!) { '
                             'commentCreate(input: {issueId: $id, body: $body}) '
                             '{ success } }',
                    'variables': {'id': '${item.ref.id}', 'body': '${params.body}'},
                },
            }],
        },
    },
    'limits': {'global': {'max_events': 60, 'window_seconds': 60}},
}


ALL = {'github': GITHUB, 'jira': JIRA, 'linear': LINEAR}


# ── tiny HTTP fixture ──────────────────────────────────────────────────────
class FakeHTTP:
    """Records requests and replays canned responses in order.

    Matches safe_http.fetch's signature, which is the same callable server.py
    injects in production — so an engine test exercises the real code path with
    only the socket removed."""

    def __init__(self, responses):
        # responses: list of (status, headers, body_bytes)
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, *, method='GET', headers=None, body=None,
                 timeout=None):
        self.calls.append({'url': url, 'method': method,
                           'headers': dict(headers or {}), 'body': body})
        if not self.responses:
            raise AssertionError(
                f'FakeHTTP ran out of responses at call {len(self.calls)}: '
                f'{method} {url}')
        return self.responses.pop(0)


# ── A workspace that is actually configured ────────────────────────────────
#
# A board run refuses to start when the dashboard MCP could not authenticate
# (#633): otherwise every dispatched agent rediscovers the 401 on its own,
# burns a build reasoning about it, and reports it as a per-item disposition.
#
# That guard means any suite which CREATES runs needs a workspace whose task
# API token exists. Without it the suites pass only on a machine that happens
# to have a real /home/dev/.claude-tasks/.api-token — and fail on every CI
# runner. That is not hypothetical: the guard landed with only one of the four
# board suites adjusted, and CI failed with 64 failures and 29 errors while the
# same tests passed locally.
#
# It lives here, in the module all four suites already share, so the next board
# suite inherits it instead of rediscovering this.

import atexit as _atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile
from unittest import mock as _mock

_TOKEN_DIR = None


def workspace_token_patch():
    """A patcher pinning `ClaudeTaskManager.TOKEN_FILE` at a real token file.

    Use from `setUpClass`, which keeps it symmetric with the other class-scoped
    patches these suites already do:

        p = fx.workspace_token_patch()
        p.start()
        cls.addClassCleanup(p.stop)
    """
    global _TOKEN_DIR
    import server

    if _TOKEN_DIR is None:
        _TOKEN_DIR = _tempfile.mkdtemp(prefix='kc-fx-token-')
        _atexit.register(_shutil.rmtree, _TOKEN_DIR, True)
        with open(_os.path.join(_TOKEN_DIR, '.api-token'), 'w') as fh:
            fh.write('test-api-token')
    return _mock.patch.object(server.ClaudeTaskManager, 'TOKEN_FILE',
                              _os.path.join(_TOKEN_DIR, '.api-token'))
