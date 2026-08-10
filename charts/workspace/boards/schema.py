"""Connector schema validation — pure, stdlib only, no jsonschema.

A connector describes how to talk to ONE external board (a Jira project, a
GitHub repo's issues, a Zendesk queue) as DATA. This module is the closed gate
that data must pass before the engine will execute it.

Why validation is the security boundary and not a nicety:

- The connector is authored by an AI agent that reads third-party API docs
  (#559: those docs are untrusted input). If the schema accepted arbitrary
  keys, an instruction smuggled into a vendor doc could add an action.
- Every URL in a connector becomes an outbound request from the workspace pod.
  Interpolation is therefore restricted to a CLOSED set of tokens — there is no
  `${env.*}`, so a connector cannot exfiltrate the pod environment into a query
  string.
- The action list IS the allowlist. An agent invokes named actions with
  parameters; it cannot construct requests.

Validation returns ALL errors rather than the first one. server.py's other
managers return a single error string (ProjectsManager._normalize), which is
right for a small flat config edited by a human; a connector is a large nested
config authored in a generate → verify → correct loop, and handing the agent
one error per round trip makes that loop needlessly long.
"""

import re
import urllib.parse

# ── Closed vocabularies ────────────────────────────────────────────────────
# Deliberately tiny. A four-value status enum survives arbitrary vendor
# workflows precisely because it is small and is never the only copy of the
# truth — the vendor's own value is always retained in `raw`, and an unmapped
# value passes through as raw rather than being coerced into a bucket it does
# not belong in.
STATUSES = ('OPEN', 'IN_PROGRESS', 'ON_HOLD', 'CLOSED')
PRIORITIES = ('URGENT', 'HIGH', 'NORMAL', 'LOW')

# Five genuinely different pagination regimes. See engine._paginate.
PAGINATION_KINDS = ('page_token', 'cursor', 'next_url', 'link_header', 'offset')

AUTH_KINDS = ('none', 'bearer', 'basic', 'header')
METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')

# Credential references. The VALUE never appears in a connector — a connector
# renders in the UI and gets pasted into issues.
#   @workspace-github        the brokered GitHub App installation token
#   @board-creds/<NAME>      the board credential store (server.py)
#   @provider-keys/<NAME>    LEGACY: the provider-keys store, whose values are
#                            injected into every CLI subprocess env — so a
#                            board credential there is visible to every agent.
CRED_WORKSPACE_GITHUB = '@workspace-github'
_CRED_BOARD_RE = re.compile(r'^@board-creds/([A-Z][A-Z0-9_]{2,63})$')
_CRED_PROVIDER_RE = re.compile(r'^@provider-keys/([A-Z][A-Z0-9_]{2,63})$')

# Board ids that would be shadowed by a sibling route (/api/boards/credentials,
# /api/boards/draft). Reserving them keeps the URL space unambiguous instead of
# leaving a board silently unreachable.
# Ids that are already ROUTES under /api/boards/, so a board owning one would
# be permanently unreachable. Extend this in the same commit as any new
# /api/boards/<word> route — a board created before the route exists keeps
# working locally and 404s the day the route ships.
RESERVED_BOARD_IDS = ('credentials', 'draft', 'runs', 'templates',
                      'strategies', 'metrics')

# Item fields whose value is a {normalized, raw} pair.
ENUM_FIELDS = {'status': STATUSES, 'priority': PRIORITIES}

# Sub-object map fields and the keys each accepts.
_OBJECT_FIELDS = {
    'ref': None,  # free-form: whatever the action URLs need to interpolate
    'assignee': ('id', 'name', 'email'),
    'contact': ('id', 'name', 'email'),
    'collection': ('id', 'name'),
}

_SCALAR_MAP_FIELDS = (
    'id', 'key', 'title', 'body', 'status_raw', 'priority_raw', 'tags', 'url',
    'created_at', 'updated_at',
)

_TOKEN_RE = re.compile(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)((?:\.[a-zA-Z0-9_]+)*)\}')
_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
_STEP_ID_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
_ACTION_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')

MAX_STEPS_PER_ACTION = 6
MAX_ACTIONS = 24
DEFAULT_MAX_PAGES = 20
MAX_MAX_PAGES = 200


def _is_str(v):
    return isinstance(v, str)


def tokens_in(value):
    """Every `${...}` token root+path found anywhere inside `value` (str, list
    or dict). Returns a list of (root, dotted_rest) pairs."""
    out = []
    if _is_str(value):
        for m in _TOKEN_RE.finditer(value):
            out.append((m.group(1), (m.group(2) or '').lstrip('.')))
    elif isinstance(value, list):
        for v in value:
            out.extend(tokens_in(v))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.extend(tokens_in(k))
            out.extend(tokens_in(v))
    return out


def _check_tokens(value, allowed_roots, errors, where):
    """Reject any interpolation token whose ROOT is outside the allowed set.

    This is what stops `${env.ANTHROPIC_API_KEY}` or `${creds.token}` appearing
    in a URL — the engine would not resolve them, but rejecting at validation
    time means a malicious connector never reaches the engine at all."""
    for root, _rest in tokens_in(value):
        if root not in allowed_roots:
            errors.append(
                f'{where}: unknown interpolation token ${{{root}}} '
                f'(allowed: {", ".join(sorted(allowed_roots))})')


def _check_url_template(value, errors, where):
    """A URL may be a template, so it cannot always be parsed. What we CAN
    insist on is that it is absolute http(s) once the leading `${base_url}` is
    accounted for — a relative or scheme-less URL is a bug that would otherwise
    surface as a confusing runtime failure."""
    if not _is_str(value) or not value.strip():
        errors.append(f'{where}: url is required and must be a string')
        return
    v = value.strip()
    if v.startswith('${base_url}'):
        return
    parsed = urllib.parse.urlparse(v)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        errors.append(
            f'{where}: url must be absolute http(s) or start with ${{base_url}} '
            f'(got {v[:60]!r})')


def _validate_request(req, errors, where, allowed_roots):
    """A transport-neutral request: {method, url, headers, query, body}.

    `body` is a full JSON value rather than a string precisely so GraphQL works
    — Monday, Linear and GitHub Projects v2 put the operation in the body, and
    a schema that assumes the URL encodes the operation excludes them."""
    if not isinstance(req, dict):
        errors.append(f'{where}: request must be an object')
        return
    method = req.get('method', 'GET')
    if method not in METHODS:
        errors.append(f'{where}.method must be one of {METHODS}')
    _check_url_template(req.get('url'), errors, f'{where}.url')
    for key in ('headers', 'query'):
        val = req.get(key)
        if val is None:
            continue
        if not isinstance(val, dict) or not all(
                _is_str(k) and _is_str(v) for k, v in val.items()):
            errors.append(f'{where}.{key} must be an object of string->string')
    _check_tokens(
        {k: v for k, v in req.items() if k != 'method'}, allowed_roots, errors, where)


def _validate_pagination(pg, errors, where):
    """The tagged union. Each kind carries only the fields it needs; unknown
    fields are rejected so a typo'd `token_path` cannot silently disable
    pagination and truncate a board."""
    if not isinstance(pg, dict):
        errors.append(f'{where}: pagination must be an object')
        return
    kind = pg.get('kind')
    if kind not in PAGINATION_KINDS:
        errors.append(f'{where}.kind must be one of {PAGINATION_KINDS}')
        return

    required = {
        'page_token': ('token_path', 'into'),
        'cursor': ('cursor_path', 'into'),
        'next_url': ('next_path',),
        'link_header': (),
        'offset': ('offset_param',),
    }[kind]
    optional = {
        'page_token': (),
        'cursor': ('has_more_path',),
        'next_url': (),
        'link_header': ('rel',),
        'offset': ('limit_param', 'total_path', 'start_at'),
    }[kind]

    for field in required:
        if not pg.get(field):
            errors.append(f'{where}.{field} is required for kind={kind!r}')
    allowed = {'kind'} | set(required) | set(optional)
    for field in pg:
        if field not in allowed:
            errors.append(f'{where}: unknown field {field!r} for kind={kind!r}')

    # `into` says WHERE the next-page value goes. Supporting `body` is what
    # makes GraphQL cursors expressible at all.
    into = pg.get('into')
    if kind in ('page_token', 'cursor'):
        if not isinstance(into, dict) or len(into) != 1:
            errors.append(
                f'{where}.into must be an object with exactly one of '
                f'"query" or "body"')
        else:
            slot, path = next(iter(into.items()))
            if slot not in ('query', 'body'):
                errors.append(f'{where}.into key must be "query" or "body"')
            if not _is_str(path) or not path:
                errors.append(f'{where}.into value must be a non-empty path')


def _validate_map(mp, errors, where):
    """The vendor→canonical field mapping.

    `id` is required and is the GLOBAL identity: idempotency markers key on it.
    `key` and `ref` are deliberately separate — GitLab issues carry a global
    `id` (46) AND a project-scoped `iid` (5), fetched as
    /projects/42/issues/5. Conflating them makes processed-markers collide
    across projects, which is a silent double-processing bug."""
    if not isinstance(mp, dict):
        errors.append(f'{where}: map must be an object')
        return
    if not mp.get('id'):
        errors.append(f'{where}.id is required (the stable GLOBAL item identity)')

    known = set(_SCALAR_MAP_FIELDS) | set(_OBJECT_FIELDS)
    for field, spec in mp.items():
        if field not in known:
            errors.append(f'{where}: unknown map field {field!r}')
            continue
        if field in _OBJECT_FIELDS:
            if not isinstance(spec, dict):
                errors.append(f'{where}.{field} must be an object')
                continue
            allowed_keys = _OBJECT_FIELDS[field]
            for sub, subspec in spec.items():
                if allowed_keys is not None and sub not in allowed_keys:
                    errors.append(
                        f'{where}.{field}: unknown key {sub!r} '
                        f'(allowed: {", ".join(allowed_keys)})')
                _validate_field_spec(subspec, errors, f'{where}.{field}.{sub}')
        else:
            _validate_field_spec(spec, errors, f'{where}.{field}')


def _validate_field_spec(spec, errors, where):
    """One mapped field. Three forms, all declarative:
        "a.b.c"                        dotted path into the vendor object
        {"template": "${base_url}/x"}  interpolated string
        {"join": [...], "sep": "+"}    composite — this is what makes GitHub's
                                       state + state_reason round-trip work
    """
    if _is_str(spec):
        return
    if not isinstance(spec, dict):
        errors.append(f'{where}: must be a path string or an object')
        return
    if 'template' in spec:
        if not _is_str(spec['template']):
            errors.append(f'{where}.template must be a string')
        _check_tokens(spec['template'], {'base_url', 'item', 'raw'}, errors, where)
        extra = set(spec) - {'template'}
        if extra:
            errors.append(f'{where}: unexpected keys with template: {sorted(extra)}')
        return
    if 'join' in spec:
        if not isinstance(spec['join'], list) or not spec['join'] or \
                not all(_is_str(p) for p in spec['join']):
            errors.append(f'{where}.join must be a non-empty list of paths')
        if 'sep' in spec and not _is_str(spec['sep']):
            errors.append(f'{where}.sep must be a string')
        extra = set(spec) - {'join', 'sep'}
        if extra:
            errors.append(f'{where}: unexpected keys with join: {sorted(extra)}')
        return
    errors.append(f'{where}: object form must have "template" or "join"')


def _validate_enums(enums, errors, where):
    """Vendor value → normalized bucket. Only the closed sets are accepted as
    bucket names; anything the connector does not list simply passes through
    unmapped, which is the point (see module docstring)."""
    if not isinstance(enums, dict):
        errors.append(f'{where}: enums must be an object')
        return
    for field, mapping in enums.items():
        if field not in ENUM_FIELDS:
            errors.append(
                f'{where}: unknown enum field {field!r} '
                f'(allowed: {", ".join(sorted(ENUM_FIELDS))})')
            continue
        allowed = ENUM_FIELDS[field]
        if not isinstance(mapping, dict):
            errors.append(f'{where}.{field} must be an object')
            continue
        for bucket, raws in mapping.items():
            if bucket not in allowed:
                errors.append(
                    f'{where}.{field}: {bucket!r} is not a valid normalized '
                    f'value (allowed: {", ".join(allowed)})')
            if not isinstance(raws, list) or not all(_is_str(r) for r in raws):
                errors.append(
                    f'{where}.{field}.{bucket} must be a list of raw vendor strings')


def _validate_actions(actions, errors, where):
    """Named, multi-step, declarative actions — the write allowlist.

    A one-shot {method,url,body} cannot express Jira, and Jira is the board
    that matters most: changing a status means GET the available transitions
    (the set is status- and permission-dependent, so ids are not stable), find
    the one whose to.name matches, then POST that id. `select` is the minimum
    expressive power that reality requires, and it is still data.
    """
    if not isinstance(actions, dict):
        errors.append(f'{where}: actions must be an object')
        return
    if len(actions) > MAX_ACTIONS:
        errors.append(f'{where}: too many actions (max {MAX_ACTIONS})')

    for name, act in actions.items():
        aw = f'{where}.{name}'
        if not _ACTION_NAME_RE.match(name or ''):
            errors.append(f'{where}: invalid action name {name!r}')
            continue
        if not isinstance(act, dict):
            errors.append(f'{aw}: must be an object')
            continue

        params = act.get('params') or {}
        if not isinstance(params, dict):
            errors.append(f'{aw}.params must be an object')
            params = {}
        for pname, pspec in params.items():
            if not isinstance(pspec, dict) or pspec.get('type') not in (
                    'string', 'number', 'boolean'):
                errors.append(
                    f'{aw}.params.{pname}.type must be string|number|boolean')

        writes = act.get('writes', 1)
        if not isinstance(writes, int) or writes < 0:
            errors.append(f'{aw}.writes must be a non-negative integer')

        steps = act.get('steps')
        if not isinstance(steps, list) or not steps:
            errors.append(f'{aw}.steps must be a non-empty list')
            continue
        if len(steps) > MAX_STEPS_PER_ACTION:
            errors.append(f'{aw}.steps: too many steps (max {MAX_STEPS_PER_ACTION})')

        # Step ids come into scope for LATER steps only, so a step cannot
        # reference itself or a step that has not run yet.
        seen_ids = set()
        select_aliases = set()
        for i, step in enumerate(steps):
            sw = f'{aw}.steps[{i}]'
            if not isinstance(step, dict):
                errors.append(f'{sw}: must be an object')
                continue
            sid = step.get('id')
            if sid is not None:
                if not _STEP_ID_RE.match(sid):
                    errors.append(f'{sw}.id must match [a-z][a-z0-9_]*')
                elif sid in seen_ids:
                    errors.append(f'{sw}.id {sid!r} is already used')
                else:
                    seen_ids.add(sid)

            sel = step.get('select')
            if sel is not None:
                if not isinstance(sel, dict):
                    errors.append(f'{sw}.select must be an object')
                else:
                    frm = sel.get('from')
                    alias = sel.get('as')
                    where_cl = sel.get('where')
                    if not _is_str(frm) or not frm:
                        errors.append(f'{sw}.select.from is required')
                    else:
                        root = frm.split('.')[0]
                        if root not in seen_ids:
                            errors.append(
                                f'{sw}.select.from refers to step {root!r}, '
                                f'which is not a PRIOR step id '
                                f'(known: {sorted(seen_ids) or "none"})')
                    if not _is_str(alias) or not _STEP_ID_RE.match(alias or ''):
                        errors.append(f'{sw}.select.as must match [a-z][a-z0-9_]*')
                    else:
                        select_aliases.add(alias)
                    if not isinstance(where_cl, dict) or not where_cl:
                        errors.append(f'{sw}.select.where must be a non-empty object')
                    else:
                        _check_tokens(where_cl, {'item', 'params', 'base_url'},
                                      errors, f'{sw}.select.where')

            allowed_roots = ({'base_url', 'item', 'params', 'marker'}
                             | seen_ids | select_aliases)
            _validate_request(step, errors, sw, allowed_roots)
            for field in step:
                if field not in ('id', 'method', 'url', 'headers', 'query',
                                 'body', 'select'):
                    errors.append(f'{sw}: unknown field {field!r}')

        idem = act.get('idempotency')
        if idem is not None:
            _validate_idempotency(idem, errors, f'{aw}.idempotency')

        for field in act:
            if field not in ('params', 'writes', 'steps', 'idempotency',
                             'description', 'passthrough'):
                errors.append(f'{aw}: unknown field {field!r}')


def _validate_idempotency(idem, errors, where):
    """Actions differ in natural idempotency. Setting a status twice is
    harmless; posting a comment twice is a visible mistake in front of a
    customer. Comment-type actions therefore embed a stable marker so a retry
    can detect our own prior comment before posting again."""
    if not isinstance(idem, dict):
        errors.append(f'{where}: must be an object')
        return
    marker = idem.get('marker_template')
    if not _is_str(marker) or '${' not in (marker or ''):
        errors.append(f'{where}.marker_template must be a template string')
    else:
        _check_tokens(marker, {'board_id', 'item', 'action_hash'}, errors, where)
    probe = idem.get('probe')
    if probe is not None:
        _validate_request(probe, errors, f'{where}.probe',
                          {'base_url', 'item', 'params'})
        if not _is_str(idem.get('probe_items_path')):
            errors.append(f'{where}.probe_items_path is required with a probe')
        if not _is_str(idem.get('probe_field')):
            errors.append(f'{where}.probe_field is required with a probe')


def _validate_review(review, actions, errors, where):
    """Per-board review behaviour (#588 Phase 6).

    Today just the re-scoping round trip's opt-in: when an agent reports
    `needs_rescoping`, optionally ask its question ON the source ticket, so the
    requester answers where they already work rather than in a queue they
    cannot see.

    Default OFF, deliberately. A board whose requesters are colleagues wants
    this; a board whose requesters are paying customers very likely does not,
    and "the robot asked my customer a question" is not a setting anyone should
    acquire by accident.
    """
    if not isinstance(review, dict):
        errors.append(f'{where} must be an object')
        return
    for field in review:
        if field not in ('ask_on_source', 'ask_action'):
            errors.append(f'{where}: unknown field {field!r} '
                          f'(allowed: ask_action, ask_on_source)')

    ask = review.get('ask_on_source', False)
    if not isinstance(ask, bool):
        errors.append(f'{where}.ask_on_source must be a boolean')
        ask = False

    action = review.get('ask_action')
    if action is not None and not _is_str(action):
        errors.append(f'{where}.ask_action must be a string')
    elif ask:
        # The action list IS the allowlist. Letting this name something the
        # connector does not declare would be a second, quieter way to invoke
        # a write — exactly what the allowlist exists to prevent.
        if not action:
            errors.append(f'{where}.ask_action is required when ask_on_source '
                          f'is on — name the action that posts the question')
        elif action not in (actions or {}):
            errors.append(
                f'{where}.ask_action {action!r} is not declared by this '
                f'connector (declared: {", ".join(sorted(actions or {})) or "none"})')


def _validate_limits(limits, errors, where):
    """Three genuinely different regimes, so a single requests_per_minute is
    the wrong shape: Zendesk is account-wide by plan BUT caps ticket updates at
    30 per 10 min per user per ticket; Asana allocates per token; GitHub's
    secondary limits return 200 or 403 rather than 429."""
    if not isinstance(limits, dict):
        errors.append(f'{where}: limits must be an object')
        return
    for key in ('global', 'per_action'):
        val = limits.get(key)
        if val is None:
            continue
        buckets = {'_': val} if key == 'global' else val
        if not isinstance(buckets, dict):
            errors.append(f'{where}.{key} must be an object')
            continue
        for bname, bucket in buckets.items():
            bw = f'{where}.{key}' + ('' if key == 'global' else f'.{bname}')
            if not isinstance(bucket, dict):
                errors.append(f'{bw} must be an object')
                continue
            for field in ('max_events', 'window_seconds'):
                v = bucket.get(field)
                if not isinstance(v, (int, float)) or v <= 0:
                    errors.append(f'{bw}.{field} must be a positive number')
            for field in bucket:
                if field not in ('max_events', 'window_seconds'):
                    errors.append(f'{bw}: unknown field {field!r}')

    piw = limits.get('per_item_writes')
    if piw is not None and (not isinstance(piw, int) or piw <= 0):
        errors.append(f'{where}.per_item_writes must be a positive integer')

    # The per-item budget is a sliding WINDOW, not a lifetime total — Zendesk's
    # is 30 per 10 minutes per ticket. A lifetime cap would silently retire a
    # busy ticket forever, which is a far worse failure than throttling.
    piww = limits.get('per_item_writes_window_seconds')
    if piww is not None and (not isinstance(piww, (int, float)) or piww <= 0):
        errors.append(
            f'{where}.per_item_writes_window_seconds must be a positive number')

    rah = limits.get('retry_after_headers')
    if rah is not None and (not isinstance(rah, list)
                            or not all(_is_str(h) for h in rah)):
        errors.append(f'{where}.retry_after_headers must be a list of header names')

    det = limits.get('limit_detect')
    if det is not None:
        if not isinstance(det, dict):
            errors.append(f'{where}.limit_detect must be an object')
        else:
            st = det.get('statuses')
            if st is not None and (not isinstance(st, list)
                                   or not all(isinstance(s, int) for s in st)):
                errors.append(f'{where}.limit_detect.statuses must be a list of ints')
            bc = det.get('body_contains')
            if bc is not None and (not isinstance(bc, list)
                                   or not all(_is_str(s) for s in bc)):
                errors.append(
                    f'{where}.limit_detect.body_contains must be a list of strings')
            for field in det:
                if field not in ('statuses', 'body_contains'):
                    errors.append(f'{where}.limit_detect: unknown field {field!r}')

    for field in limits:
        if field not in ('global', 'per_action', 'per_item_writes',
                         'per_item_writes_window_seconds',
                         'retry_after_headers', 'limit_detect'):
            errors.append(f'{where}: unknown field {field!r}')


def validate_credential_ref(ref):
    """(ok, error). A connector NEVER carries a secret value — it renders in
    the UI and gets pasted into issues. It names one instead, and server.py
    resolves the name at request time."""
    if ref is None or ref == '':
        return True, None       # a public board needs no credential
    if not _is_str(ref):
        return False, 'credential_ref must be a string'
    if ref == CRED_WORKSPACE_GITHUB:
        return True, None
    if _CRED_BOARD_RE.match(ref) or _CRED_PROVIDER_RE.match(ref):
        return True, None
    if not ref.startswith('@'):
        return False, (
            'credential_ref must be a reference like "@workspace-github" or '
            '"@board-creds/NAME" — never an inline secret')
    return False, f'unknown credential reference {ref!r}'


def validate_connector(cfg, *, require_actions=False):
    """Validate a connector config. Returns `(cleaned, errors)`.

    `cleaned` is None when there are errors. When it is not None it is a NEW
    dict containing only known keys with defaults applied — the input is never
    passed through, mirroring DesktopManager._validate's discipline of building
    a cleaned value from an allowlist rather than trusting what arrived.
    """
    errors = []
    if not isinstance(cfg, dict):
        return None, ['connector must be an object']

    version = cfg.get('version', 1)
    if version != 1:
        errors.append('version must be 1')

    display_name = (cfg.get('display_name') or '').strip()
    if not display_name:
        errors.append('display_name is required')
    elif len(display_name) > 120:
        errors.append('display_name must be 120 characters or fewer')

    vendor = (cfg.get('vendor') or '').strip()
    if not vendor:
        errors.append('vendor is required (e.g. "jira", "github", "linear")')
    elif not _ID_RE.match(vendor):
        errors.append('vendor must be 1-64 chars of [A-Za-z0-9_-]')

    base_url = (cfg.get('base_url') or '').strip().rstrip('/')
    if not base_url:
        errors.append('base_url is required')
    else:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            errors.append('base_url must be an absolute http(s) URL')

    ok, err = validate_credential_ref(cfg.get('credential_ref'))
    if not ok:
        errors.append(err)

    auth = cfg.get('auth') or {'kind': 'none'}
    if not isinstance(auth, dict):
        errors.append('auth must be an object')
        auth = {'kind': 'none'}
    else:
        kind = auth.get('kind', 'none')
        if kind not in AUTH_KINDS:
            errors.append(f'auth.kind must be one of {AUTH_KINDS}')
        if kind == 'header':
            if not auth.get('header'):
                errors.append('auth.header is required for kind="header"')
            if not auth.get('template'):
                errors.append('auth.template is required for kind="header"')
        if auth.get('template') is not None:
            _check_tokens(auth['template'], {'credential'}, errors, 'auth.template')
        for field in auth:
            if field not in ('kind', 'header', 'template'):
                errors.append(f'auth: unknown field {field!r}')
        if kind != 'none' and not cfg.get('credential_ref'):
            errors.append(f'auth.kind={kind!r} requires a credential_ref')

    allow_internal = cfg.get('allow_internal', False)
    if not isinstance(allow_internal, bool):
        errors.append('allow_internal must be a boolean')

    lst = cfg.get('list')
    if not isinstance(lst, dict):
        errors.append('list is required and must be an object')
        lst = {}
    else:
        _validate_request(lst.get('request'), errors, 'list.request',
                          {'base_url', 'page'})
        # The key must be PRESENT, but "" is a legitimate value meaning "the
        # response body IS the array" — GitHub's issues endpoint returns a
        # top-level array. Requiring presence rather than truthiness keeps that
        # expressible without letting a missing/typo'd key silently mean root.
        if 'items_path' not in lst or not _is_str(lst.get('items_path')):
            errors.append(
                'list.items_path is required (dotted path to the array; '
                'use "" when the response body is itself the array)')
        page_size = lst.get('page_size', 50)
        if not isinstance(page_size, int) or not (1 <= page_size <= 1000):
            errors.append('list.page_size must be an integer between 1 and 1000')
        max_pages = lst.get('max_pages', DEFAULT_MAX_PAGES)
        if not isinstance(max_pages, int) or not (1 <= max_pages <= MAX_MAX_PAGES):
            errors.append(
                f'list.max_pages must be an integer between 1 and {MAX_MAX_PAGES}')
        _validate_pagination(lst.get('pagination'), errors, 'list.pagination')
        for field in lst:
            if field not in ('request', 'items_path', 'page_size', 'max_pages',
                             'pagination'):
                errors.append(f'list: unknown field {field!r}')

    _validate_map(cfg.get('map'), errors, 'map')

    if cfg.get('enums') is not None:
        _validate_enums(cfg['enums'], errors, 'enums')

    actions = cfg.get('actions')
    if actions is None:
        if require_actions:
            errors.append('actions is required')
        actions = {}
    else:
        _validate_actions(actions, errors, 'actions')

    limits = cfg.get('limits')
    if limits is not None:
        _validate_limits(limits, errors, 'limits')

    review = cfg.get('review')
    if review is not None:
        _validate_review(review, actions, errors, 'review')

    for field in cfg:
        if field not in ('id', 'version', 'vendor', 'display_name', 'base_url',
                         'credential_ref', 'auth', 'allow_internal', 'list',
                         'map', 'enums', 'actions', 'limits', 'review',
                         'created_at', 'updated_at'):
            errors.append(f'unknown field {field!r}')

    if errors:
        return None, errors

    cleaned = {
        'version': 1,
        'vendor': vendor,
        'display_name': display_name,
        'base_url': base_url,
        'credential_ref': cfg.get('credential_ref') or '',
        'review': dict(review or {}),
        'auth': {'kind': auth.get('kind', 'none')},
        'allow_internal': bool(allow_internal),
        'list': {
            'request': dict(lst['request']),
            'items_path': lst['items_path'],
            'page_size': lst.get('page_size', 50),
            'max_pages': lst.get('max_pages', DEFAULT_MAX_PAGES),
            'pagination': dict(lst['pagination']),
        },
        'map': dict(cfg['map']),
        'enums': dict(cfg.get('enums') or {}),
        'actions': dict(actions),
        'limits': dict(limits or {}),
    }
    if auth.get('header'):
        cleaned['auth']['header'] = auth['header']
    if auth.get('template'):
        cleaned['auth']['template'] = auth['template']
    for stamp in ('id', 'created_at', 'updated_at'):
        if cfg.get(stamp):
            cleaned[stamp] = cfg[stamp]
    return cleaned, []


def public_view(cfg):
    """The connector as the UI may see it. A connector holds no secret VALUE by
    construction (only a credential_ref), so this is mostly identity — but it
    is a real function rather than a pass-through so that the day someone adds
    a field that could carry a secret, there is one obvious place to redact it,
    and so `credential_set` can report resolvability without leaking anything."""
    if not isinstance(cfg, dict):
        return {}
    out = dict(cfg)
    ref = out.get('credential_ref') or ''
    out['credential_ref'] = ref
    out['credential_set'] = bool(ref)
    return out


def action_names(cfg):
    """The write allowlist for this connector. An agent may invoke these names
    and nothing else."""
    actions = (cfg or {}).get('actions') or {}
    return sorted(actions.keys())
