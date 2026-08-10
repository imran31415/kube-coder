"""Starter connectors (#588 Phase 7) — GitHub Issues, Jira Cloud, Zendesk.

The epic's issue breakdown asks for *"docs + worked adapter examples (GitHub,
Jira, Zendesk)"* and *"customer-service connector templates"*. This is that:
three connectors already written against each vendor's real API, offered at
board-create time so nobody starts from an empty JSON object.

**A template is not a verified connector, and must never be presented as one.**
It is a starting point with the vendor-specific hard parts already solved —
GitHub's `Link` pagination and its `state`+`state_reason` composite, Jira's
two-step transition, Zendesk's `next_page` and its per-ticket write cap. What
it cannot know is your repo, your project key, your subdomain or whether your
credential works. Only `test-fetch` can answer that, and only running it earns
the word "verified" — the same discipline `BOARD_GEN_PREAMBLE` already imposes
on an agent authoring one from scratch.

Every template therefore ships with `${...}` placeholders in exactly the places
a human must fill in, listed in `needs`, and validates cleanly against
`schema.validate_connector` — a template that could not be saved would be a
worse starting point than a blank page.

All three have now been exercised against real vendor accounts;
`tests/board_fixtures.py` uses the same shapes and a test asserts they have not
drifted apart. Production must not import from `tests/`.
"""
import copy
import re

# ── GitHub Issues ──────────────────────────────────────────────────────────
# Uses the workspace's brokered GitHub App token, so there is no PAT to paste.
# Secondary rate limits arrive as 200 or 403 rather than 429, so `limit_detect`
# lists those statuses AND requires a body phrase — otherwise an ordinary
# permission 403 would be retried forever as if it were throttling.
GITHUB_ISSUES = {
    'vendor': 'github',
    'display_name': 'GitHub Issues',
    'base_url': 'https://api.github.com',
    'credential_ref': '@workspace-github',
    'auth': {'kind': 'bearer'},
    'list': {
        'request': {
            'method': 'GET',
            'url': '${base_url}/repos/OWNER/REPO/issues',
            'query': {'state': 'open', 'per_page': '50'},
            'headers': {'Accept': 'application/vnd.github+json'},
        },
        'items_path': '',              # the response body IS the array
        'page_size': 50,
        'max_pages': 20,
        # GitHub keeps sending rel="prev"/"first" on the final page, so a Link
        # header with no `next` is a positive terminator rather than missing
        # metadata. The engine distinguishes those; a connector need not.
        'pagination': {'kind': 'link_header', 'rel': 'next'},
    },
    'map': {
        'id': 'id',                    # global id — processed markers key on it
        'key': 'number',               # per-repo number — what humans quote
        'ref': {'owner': {'template': 'OWNER'},
                'repo': {'template': 'REPO'},
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
        # `state_reason` is joined onto `state`, so every combination the
        # vendor can emit needs covering. `open+reopened` is an ORDINARY state —
        # any issue anyone has ever reopened — and leaving it out normalized a
        # perfectly normal ticket to nothing. Found against a real repo, where
        # two of six issues were reopened.
        'status': {
            'OPEN': ['open', 'open+reopened'],
            'CLOSED': ['closed', 'closed+completed', 'closed+not_planned',
                       'closed+duplicate'],
        },
    },
    'actions': {
        'comment': {
            'params': {'body': {'type': 'string', 'required': True}},
            'writes': 1,
            'idempotency': {
                'marker_template':
                    '<!-- kc:${board_id}:${item.id}:${action_hash} -->',
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
            # `state_reason` is required for the same reason the body can
            # reference it at all: the completed / not_planned distinction is
            # the one this board processor most cares about — it is what tells
            # a later run whether an issue was solved or dropped — and GitHub's
            # own default silently records every close as "completed".
            # Reopening takes `reopened`.
            'params': {'state': {'type': 'string', 'required': True},
                       'state_reason': {'type': 'string', 'required': True}},
            'writes': 1,
            'steps': [{
                'method': 'PATCH',
                'url': '${base_url}/repos/${item.ref.owner}/${item.ref.repo}'
                       '/issues/${item.ref.number}',
                'body': {'state': '${params.state}',
                         'state_reason': '${params.state_reason}'},
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


# ── Jira Cloud ─────────────────────────────────────────────────────────────
# /rest/api/3/search was RETIRED and now answers 410 Gone; /search/jql is the
# current endpoint. It returns an opaque nextPageToken and no total.
JIRA_CLOUD = {
    'vendor': 'jira',
    'display_name': 'Jira Cloud',
    'base_url': 'https://YOURSITE.atlassian.net',
    'credential_ref': '@board-creds/JIRA_API_TOKEN',
    'auth': {'kind': 'header', 'header': 'Authorization',
             'template': 'Basic ${credential}'},
    'list': {
        'request': {
            'method': 'GET',
            'url': '${base_url}/rest/api/3/search/jql',
            'query': {
                'jql': 'project = PROJ ORDER BY updated DESC',
                'maxResults': '50',
                # NOT optional, and the omission is silent. `/search/jql`
                # returns ONLY `id` when `fields` is absent — no key, no
                # summary, no status — so every mapped field below resolves to
                # null and the board lists rows that look like empty tickets.
                # Worse, it also keeps handing back a nextPageToken, so the
                # walk does not terminate on its own. Found against a real Jira
                # site; nothing about the response is an error, which is why no
                # amount of schema validation catches it.
                'fields': ('summary,description,status,priority,assignee,'
                           'reporter,project,labels,created,updated'),
            },
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
                     'NORMAL': ['Medium', 'P3'],
                     'LOW': ['Low', 'Lowest', 'P4']},
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
                # The probe stringifies whatever this path resolves to, so it
                # still finds the marker inside the ADF document below.
                'probe_field': 'body',
            },
            'steps': [{
                'method': 'POST',
                'url': '${base_url}/rest/api/3/issue/${item.ref.issue_key}'
                       '/comment',
                # REST v3 requires ADF (Atlassian Document Format) — a
                # structured document, not a string. A plain string is refused
                # with HTTP 400 `"Comment body is not valid!"`, which is a
                # perfectly ordinary-looking failure that no schema check can
                # anticipate. Found approving a real staged comment.
                #
                # The marker gets its own paragraph rather than being appended
                # to the reply: ADF text nodes do not render "\n" as a break,
                # so concatenating would put the bookkeeping marker on the same
                # visual line as the sentence a customer reads.
                'body': {'body': {
                    'type': 'doc',
                    'version': 1,
                    'content': [
                        {'type': 'paragraph',
                         'content': [{'type': 'text',
                                      'text': '${params.body}'}]},
                        {'type': 'paragraph',
                         'content': [{'type': 'text', 'text': '${marker}'}]},
                    ],
                }},
            }],
        },
    },
    'limits': {
        'global': {'max_events': 100, 'window_seconds': 60},
        'per_action': {'comment': {'max_events': 30, 'window_seconds': 600}},
        'per_item_writes': 8,
    },
}


# ── Zendesk ────────────────────────────────────────────────────────────────
# The customer-service case, and the one whose limits shape the design: the
# account-wide budget is generous (200-2500/min by plan) but Update Ticket is
# capped at 30 per 10 minutes PER USER PER TICKET, which is exactly what
# `per_action` and `per_item_writes` exist for.
#
# Auth is an OAuth ACCESS TOKEN, sent as a bearer.
#
# The obvious choice would be Basic with the `/token` username suffix, and every
# Zendesk integration guide still shows it. It is a dead end: Zendesk has
# withdrawn API-token creation from the admin UI — a new account has no "Add API
# token" button at all — and has announced that existing tokens stop working on
# 30 April 2027. A template whose auth scheme cannot be obtained by anyone
# setting up today, and expires for everyone else, is not a starting point.
#
# So: bearer. `credential` is the access token from an OAuth client, and the
# store holds it as format "token" with nothing to compose. See NEEDS below for
# how to mint one — it is a browser flow, not a settings page.
#
# `status` is Zendesk's own vocabulary and `pending` means "waiting on the
# requester", which is why it maps to ON_HOLD rather than IN_PROGRESS: an
# agent must not treat a ticket blocked on the customer as work in flight.
ZENDESK = {
    'vendor': 'zendesk',
    'display_name': 'Zendesk Support',
    'base_url': 'https://YOURSUBDOMAIN.zendesk.com',
    'credential_ref': '@board-creds/ZENDESK_OAUTH_TOKEN',
    'auth': {'kind': 'bearer'},
    'list': {
        'request': {
            'method': 'GET',
            'url': '${base_url}/api/v2/tickets.json',
            'query': {'per_page': '50', 'sort_by': 'updated_at',
                      'sort_order': 'asc'},
            'headers': {'Accept': 'application/json'},
        },
        'items_path': 'tickets',
        'page_size': 50,
        'max_pages': 20,
        # Zendesk returns an ABSOLUTE next_page url, and null on the last page.
        'pagination': {'kind': 'next_url', 'next_path': 'next_page'},
    },
    'map': {
        'id': 'id',
        'key': 'id',
        'ref': {'ticket_id': 'id'},
        'title': 'subject',
        'body': 'description',
        'status_raw': 'status',
        'priority_raw': 'priority',
        'assignee': {'id': 'assignee_id'},
        # The external requester is first-class on a customer-service board —
        # not a custom field.
        'contact': {'id': 'requester_id'},
        'collection': {'id': 'group_id'},
        'tags': 'tags',
        'url': {'template': '${base_url}/agent/tickets/${item.key}'},
        'created_at': 'created_at',
        'updated_at': 'updated_at',
    },
    'enums': {
        # `new` is untriaged and `open` is assigned-and-being-worked, which is
        # the OPEN/IN_PROGRESS distinction exactly. Listing `open` under BOTH
        # would not express "either" — `_normalize_enum` takes the first bucket
        # that matches, so the second is simply unreachable, and a dead branch
        # in a template is worse than an absent one because it reads as covered.
        'status': {'OPEN': ['new'],
                   'IN_PROGRESS': ['open'],
                   'ON_HOLD': ['pending', 'hold'],
                   'CLOSED': ['solved', 'closed']},
        'priority': {'URGENT': ['urgent'], 'HIGH': ['high'],
                     'NORMAL': ['normal'], 'LOW': ['low']},
    },
    'actions': {
        # A public comment is visible to the CUSTOMER. `public` is a declared
        # parameter rather than hardcoded so an agent must choose deliberately,
        # and the default in the review card is the safer one.
        'comment': {
            # `public` is REQUIRED, not optional-with-a-default. Two reasons,
            # and the second is the one that bites: a body interpolates
            # strictly, so an absent `${params.public}` does not fall back to
            # anything — it raises, and it raises at APPROVE time, in front of
            # the reviewer, long after the agent that omitted it stopped
            # running. Required means the agent is told at once, by the tool it
            # just called. And a customer-visible reply is not a thing to have
            # a default for.
            'params': {'body': {'type': 'string', 'required': True},
                       'public': {'type': 'boolean', 'required': True}},
            'writes': 1,
            'idempotency': {
                'marker_template': '[kc:${board_id}:${item.id}:${action_hash}]',
                'probe': {'method': 'GET',
                          'url': '${base_url}/api/v2/tickets/'
                                 '${item.ref.ticket_id}/comments.json'},
                'probe_items_path': 'comments',
                'probe_field': 'body',
            },
            'steps': [{
                'method': 'PUT',
                'url': '${base_url}/api/v2/tickets/${item.ref.ticket_id}.json',
                'body': {'ticket': {'comment': {
                    'body': '${params.body}\n\n${marker}',
                    'public': '${params.public}'}}},
            }],
        },
        'set_status': {
            'params': {'status': {'type': 'string', 'required': True}},
            'writes': 1,
            'steps': [{
                'method': 'PUT',
                'url': '${base_url}/api/v2/tickets/${item.ref.ticket_id}.json',
                'body': {'ticket': {'status': '${params.status}'}},
            }],
        },
        'set_priority': {
            'params': {'priority': {'type': 'string', 'required': True}},
            'writes': 1,
            'steps': [{
                'method': 'PUT',
                'url': '${base_url}/api/v2/tickets/${item.ref.ticket_id}.json',
                'body': {'ticket': {'priority': '${params.priority}'}},
            }],
        },
    },
    'limits': {
        'global': {'max_events': 200, 'window_seconds': 60},
        # 30 per 10 minutes per user per ticket — Zendesk's documented cap on
        # Update Ticket, and the reason `per_action` is not just a global.
        'per_action': {'comment': {'max_events': 30, 'window_seconds': 600},
                       'set_status': {'max_events': 30, 'window_seconds': 600}},
        'per_item_writes': 30,
        'per_item_writes_window_seconds': 600,
    },
}


#: What a human must replace before the connector can work, in the order they
#: will meet them. Shown beside the template so "start from this" does not turn
#: into "hunt through JSON for the bits that are wrong".
NEEDS = {
    'github-issues': [
        'Replace OWNER and REPO (in list.request.url and map.ref) with your '
        'repository.',
        'Credential: none to paste — @workspace-github is the workspace\'s own '
        'GitHub App token. Its scope is the App installation.',
    ],
    'jira-cloud': [
        'Replace YOURSITE with your Atlassian site '
        '(https://<you>.atlassian.net).',
        'Replace PROJ in the JQL with your project key.',
        'Credential JIRA_API_TOKEN: format "basic", username = your Atlassian '
        'account email, token from id.atlassian.com/manage-profile/security/'
        'api-tokens.',
    ],
    'zendesk': [
        'Replace YOURSUBDOMAIN with your Zendesk subdomain (the label only: '
        '"acme" for acme.zendesk.com).',
        'Credential ZENDESK_OAUTH_TOKEN: format "token", value = an OAuth '
        'ACCESS TOKEN. API tokens do NOT work — Zendesk withdrew them from the '
        'admin UI and retires the existing ones on 2027-04-30.',
        'Mint one with the authorization-code + PKCE flow: Admin Center → Apps '
        'and integrations → APIs → OAuth clients → add a client, set any '
        'redirect URL you control, and note its Unique identifier. Then run '
        '`python3 boards/zendesk_oauth.py start <subdomain> <identifier> '
        '<redirect-url>`, approve in the browser, and feed the `code` from the '
        'redirect back with `zendesk_oauth.py finish <code>`.',
        'Do NOT use the implicit grant (response_type=token). Zendesk OAuth '
        'clients are Public or Confidential, and a Public client — what the '
        'admin UI creates — is only permitted the code+PKCE flow. The implicit '
        'URL simply refuses, which reads like a misconfigured client.',
        'Check the comment action\'s "public" parameter before approving — a '
        'public comment is visible to the customer.',
    ],
}


#: The blanks a human fills in, machine-readable so the connect form can be
#: generated from the template rather than hardcoded per vendor. `token` is the
#: literal ALL-CAPS string sitting in the connector; `fill()` replaces it.
#:
#: A test asserts every token here actually occurs in its template and that no
#: template carries a placeholder nobody declared — the failure mode otherwise
#: is a form that looks complete and produces a connector still pointing at
#: OWNER/REPO, which fails at the first fetch with a 404 nobody can read.
PLACEHOLDERS = {
    'github-issues': [
        {'token': 'OWNER', 'label': 'Owner',
         'help': 'The user or organisation the repository belongs to.',
         'example': 'acme'},
        {'token': 'REPO', 'label': 'Repository',
         'help': 'The repository name on its own, without the owner.',
         'example': 'billing-api'},
    ],
    'jira-cloud': [
        {'token': 'YOURSITE', 'label': 'Atlassian site',
         'help': 'The label only — "acme" for acme.atlassian.net.',
         'example': 'acme'},
        {'token': 'PROJ', 'label': 'Project key',
         'help': 'The key issues are prefixed with, e.g. SUP-142 → SUP.',
         'example': 'SUP'},
    ],
    'zendesk': [
        {'token': 'YOURSUBDOMAIN', 'label': 'Zendesk subdomain',
         'help': 'The label only — "acme" for acme.zendesk.com.',
         'example': 'acme'},
    ],
}


#: What the operator must paste, or None when there is nothing to paste.
#: Declared rather than derived so the wording can be vendor-specific, and
#: guarded by a test that checks it against the connector's own
#: `credential_ref` and `auth` — a form that asks for a "basic" credential
#: while the connector sends a bearer produces a 401 at the first fetch and
#: nothing at all that says why.
CREDENTIAL = {
    'github-issues': None,          # @workspace-github is brokered already
    'jira-cloud': {
        'name': 'JIRA_API_TOKEN',
        'format': 'basic',
        'username_label': 'Atlassian account email',
        'secret_label': 'API token',
        'help': 'id.atlassian.com → Security → API tokens → Create. Paste the '
                'RAW token; the server composes the Basic header.',
    },
    'zendesk': {
        'name': 'ZENDESK_OAUTH_TOKEN',
        'format': 'token',
        'username_label': '',
        'secret_label': 'OAuth access token',
        'help': 'An OAuth access token, not an API token — see the setup '
                'steps above. It starts with a long opaque string and is the '
                'value after `access_token` in the exchange response.',
    },
}

TEMPLATES = {
    'github-issues': GITHUB_ISSUES,
    'jira-cloud': JIRA_CLOUD,
    'zendesk': ZENDESK,
}


def listing():
    """What the create-a-board UI shows. Deliberately NOT the whole connector:
    a picker needs a name, what it will ask of you, and enough structure to
    render a form. The full JSON is one click further on via `get`."""
    return [
        {
            'id': tid,
            'display_name': cfg['display_name'],
            'vendor': cfg['vendor'],
            'actions': sorted(cfg.get('actions') or {}),
            'needs': NEEDS.get(tid, []),
            'placeholders': [dict(p) for p in PLACEHOLDERS.get(tid, [])],
            'credential': (dict(CREDENTIAL[tid])
                           if CREDENTIAL.get(tid) else None),
            # Said in the payload, not just in a docstring, so a client cannot
            # render a template as though it were a working connector.
            'verified': False,
            'note': 'a starting point, not a verified connector — run '
                    'test-fetch before trusting it',
        }
        for tid, cfg in sorted(TEMPLATES.items())
    ]


def get(template_id):
    """One template's full connector config, or None."""
    cfg = TEMPLATES.get(template_id)
    return {k: v for k, v in cfg.items()} if cfg else None


# A placeholder value ends up inside a URL path and, for Jira, inside a JQL
# string. Anything outside this set could rewrite the query it is spliced into
# — `PROJ` becoming `X ORDER BY created` is a different board, and a slash in
# `REPO` is a different repository. Restricting the input is the whole defence,
# because there is no meaningful way to escape a value for both a URL segment
# and JQL at once.
_VALUE_OK = re.compile(r'^[A-Za-z0-9._-]{1,100}$')


def _substitute(node, pattern, subs):
    """Replace placeholder tokens throughout a connector, strings only.

    ONE pass, via a single alternation, and that is load-bearing: a value
    substituted for OWNER may itself contain the literal text `REPO`, and a
    second pass would go on to rewrite the user's own value. `re.sub` never
    re-examines what it just inserted.
    """
    if isinstance(node, str):
        return pattern.sub(lambda m: subs[m.group(0)], node)
    if isinstance(node, dict):
        return {k: _substitute(v, pattern, subs) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, pattern, subs) for v in node]
    return node


def fill(template_id, values, board_id='', display_name=''):
    """A template plus the operator's answers → a connector ready to create.

    Returns `(connector, None)` or `(None, error)`. Pure: it saves nothing and
    contacts nobody. Whether the result actually works is a question only
    `test-fetch` answers, and the caller is expected to ask it.
    """
    cfg = TEMPLATES.get(template_id)
    if cfg is None:
        return None, f'no template {template_id!r}'

    spec = PLACEHOLDERS.get(template_id) or []
    values = values or {}
    subs = {}
    for p in spec:
        token = p['token']
        raw = str(values.get(token) or '').strip()
        if not raw:
            return None, f'{p["label"]} is required'
        if not _VALUE_OK.match(raw):
            return None, (f'{p["label"]}: use letters, digits, dot, dash or '
                          f'underscore only — got {raw!r}')
        subs[token] = raw

    out = copy.deepcopy(cfg)
    if subs:
        # Longest first so a token that is a prefix of another cannot win the
        # alternation and leave the rest of the longer one stranded.
        pattern = re.compile('|'.join(re.escape(t) for t in
                                      sorted(subs, key=len, reverse=True)))
        out = _substitute(out, pattern, subs)

    out['id'] = (board_id or '').strip() or _default_id(cfg['vendor'], spec, subs)
    if (display_name or '').strip():
        out['display_name'] = display_name.strip()
    elif subs:
        # "GitHub Issues" three times over is a rail nobody can navigate. The
        # answers the operator just gave are what distinguishes one board from
        # the next, so say those.
        out['display_name'] = f'{cfg["display_name"]} — ' + '/'.join(
            subs[p['token']] for p in spec)
    return out, None


def _default_id(vendor, spec, subs):
    """A stable, readable id when the caller did not choose one.

    `<vendor>-<the most specific answer>`: the repo, the project key, the
    subdomain. A collision is reported by the create route as a 409 rather
    than guessed around, because two boards that would share an id are two
    boards a human cannot tell apart either.
    """
    tail = subs[spec[-1]['token']] if spec else 'board'
    return re.sub(r'[^a-z0-9-]+', '-', f'{vendor}-{tail}'.lower()).strip('-')[:64]
