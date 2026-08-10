"""Deterministic connector execution — fetch, paginate, map, act.

Pure given an injected HTTP callable. This module never imports server, never
touches the filesystem, and never runs a model: an agent authors a connector at
design time, and everything here is the deterministic runtime that executes it.
That separation is the whole safety argument, so keep it.

The injected callable has the shape of `safe_http.fetch`:

    http(url, method=..., headers=..., body=..., timeout=...)
        -> (status:int, headers:dict, body:bytes)

server.py passes a partial of safe_http.fetch so that EVERY request — the list
request, every pagination `next`, and every step of every action — goes through
the same SSRF guard the completion hook uses. Tests pass a fixture, which is
why the pagination edge cases below are cheap to cover exhaustively.
"""

import hashlib
import json
import re
import urllib.parse

from . import schema

DEFAULT_TIMEOUT = 30

_TOKEN_RE = re.compile(r'\$\{([a-zA-Z_][a-zA-Z0-9_.]*)\}')
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?([a-zA-Z]+)"?')


class BoardError(Exception):
    """A connector could not be executed as written: an unresolvable token, a
    `select` that matched nothing, a vendor payload that is not JSON. Distinct
    from a transport error (which the caller sees as a status code) because it
    means the CONNECTOR is wrong, not the board."""


# ── path + template primitives ─────────────────────────────────────────────

def get_path(obj, dotted, default=None):
    """Read a dotted path out of a decoded JSON value.

    Supports numeric segments for list indexing (`items.0.id`), which is what
    lets `items_path` address deep into a GraphQL response such as
    `data.issues.nodes`. Returns `default` for any missing segment rather than
    raising — a vendor omitting an optional field is normal, not exceptional.
    """
    if not dotted:
        return obj
    cur = obj
    for seg in str(dotted).split('.'):
        if cur is None:
            return default
        if isinstance(cur, dict):
            if seg not in cur:
                return default
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def set_path(obj, dotted, value):
    """Write a dotted path into a nested dict, creating intermediate objects.
    Used to inject a GraphQL cursor into the request BODY (`variables.after`),
    which is the whole reason the transport cannot assume REST."""
    segs = str(dotted).split('.')
    cur = obj
    for seg in segs[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[segs[-1]] = value
    return obj


def interpolate(value, ctx, *, strict=True):
    """Substitute `${root.path}` tokens from `ctx`, recursively through dicts
    and lists.

    When a string is EXACTLY one token, the resolved value keeps its JSON type
    (so `{"id": "${tr.id}"}` sends a number if the vendor gave a number).

    `strict` controls what an unresolvable token means, and the two callers
    genuinely differ:

    - **Requests are strict.** A token that will not resolve raises BoardError
      rather than rendering the string "None" into a URL. A silently malformed
      request to someone else's board is far worse than a loud failure.
    - **Mapped display fields are lenient.** `url` is a deep link for the human
      reviewing a proposal; an item whose optional `key` is null is still a
      perfectly workable item, and failing the whole map would drop it from the
      board over a missing convenience field. The field resolves to None, so
      the caller renders '' — never a half-built URL like `.../browse/None`.
    """
    if isinstance(value, str):
        m = _TOKEN_RE.fullmatch(value.strip())
        if m:
            resolved = get_path(ctx, m.group(1), _MISSING)
            if resolved is _MISSING or resolved is None:
                if not strict:
                    return None
                raise BoardError(f'unresolved interpolation token ${{{m.group(1)}}}')
            return resolved

        missing = []
        for match in _TOKEN_RE.finditer(value):
            resolved = get_path(ctx, match.group(1), _MISSING)
            if resolved is _MISSING or resolved is None:
                missing.append(match.group(1))
        if missing:
            if not strict:
                return None
            raise BoardError(
                f'unresolved interpolation token ${{{missing[0]}}}')
        return _TOKEN_RE.sub(
            lambda mm: str(get_path(ctx, mm.group(1))), value)
    if isinstance(value, list):
        return [interpolate(v, ctx, strict=strict) for v in value]
    if isinstance(value, dict):
        return {interpolate(k, ctx, strict=strict):
                interpolate(v, ctx, strict=strict) for k, v in value.items()}
    return value


class _Missing:
    def __repr__(self):
        return '<missing>'


_MISSING = _Missing()


# ── request assembly ───────────────────────────────────────────────────────

def auth_headers(connector, credential):
    """Headers implementing the connector's declared auth kind.

    The credential VALUE arrives here from server.py, which resolved it from a
    reference. It is never part of the connector and never returned to a
    client."""
    auth = (connector.get('auth') or {})
    kind = auth.get('kind', 'none')
    if kind == 'none' or not credential:
        return {}
    if kind == 'bearer':
        return {'Authorization': f'Bearer {credential}'}
    if kind == 'basic':
        return {'Authorization': f'Basic {credential}'}
    if kind == 'header':
        header = auth.get('header') or 'Authorization'
        template = auth.get('template') or '${credential}'
        return {header: interpolate(template, {'credential': credential})}
    return {}


def render_request(spec, ctx, *, base_headers=None, extra_query=None,
                   body_override=None):
    """Turn a declarative request spec into concrete (method, url, headers,
    body_bytes). Query params are merged into the URL rather than passed
    separately so the caller only ever deals with a final URL — which is also
    the string the SSRF guard checks."""
    method = spec.get('method', 'GET')
    url = interpolate(spec.get('url') or '', ctx)

    query = dict(interpolate(spec.get('query') or {}, ctx))
    if extra_query:
        query.update(extra_query)
    if query:
        parsed = urllib.parse.urlparse(url)
        merged = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        merged.update({str(k): str(v) for k, v in query.items()})
        url = urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(merged)))

    headers = dict(base_headers or {})
    headers.update(interpolate(spec.get('headers') or {}, ctx))

    body_val = body_override if body_override is not None else spec.get('body')
    body_bytes = None
    if body_val is not None:
        rendered = interpolate(body_val, ctx)
        if isinstance(rendered, (dict, list)):
            body_bytes = json.dumps(rendered).encode('utf-8')
            headers.setdefault('Content-Type', 'application/json')
        else:
            body_bytes = str(rendered).encode('utf-8')
    return method, url, headers, body_bytes


def _decode(status, body, where):
    """Decode a JSON response body, or explain why we could not."""
    if not body:
        raise BoardError(f'{where}: empty response body (HTTP {status})')
    try:
        return json.loads(body.decode('utf-8', 'replace'))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        snippet = body[:180].decode('utf-8', 'replace')
        raise BoardError(f'{where}: response was not JSON (HTTP {status}): '
                         f'{e} — got {snippet!r}')


# ── pagination ─────────────────────────────────────────────────────────────

def _link_header(headers):
    """The raw RFC 5988 `Link:` value, or '' when the vendor sent none."""
    for k, v in (headers or {}).items():
        if k.lower() == 'link':
            return v or ''
    return ''


def _link_next(headers, rel='next'):
    """Parse RFC 5988 `Link:` for the given rel. GitHub's only next-page signal."""
    for url, r in _LINK_RE.findall(_link_header(headers)):
        if r.lower() == rel.lower():
            return url
    return None


def _next_page(pg, data, headers, page_items, page_size, state):
    """Decide whether another page exists.

    Returns `(verdict, value)` where verdict is one of:
      'continue' — value is the next-page state to inject
      'done'     — the vendor explicitly said this is the last page
      'absent'   — NO pagination metadata at all; the caller must decide, and
                   its decision is the correctness core (see fetch_items)

    The 'absent' verdict exists because two vendors make it a real hazard:
    Jira silently truncates a JQL query that exceeds maxResults and no longer
    returns a total, and GitLab omits X-Next-Page/X-Total-Pages entirely under
    keyset pagination (and above 10,000 records under offset pagination).
    Treating 'absent' as 'done' would silently truncate a 10,000-issue board.
    """
    kind = pg.get('kind')

    if kind == 'page_token':
        token = get_path(data, pg['token_path'])
        return ('continue', token) if token else ('absent', None)

    if kind == 'cursor':
        has_more_path = pg.get('has_more_path')
        if has_more_path:
            has_more = get_path(data, has_more_path)
            if has_more is False:
                return ('done', None)
        cursor = get_path(data, pg['cursor_path'])
        return ('continue', cursor) if cursor else ('absent', None)

    if kind == 'next_url':
        nxt = get_path(data, pg['next_path'])
        return ('continue', nxt) if nxt else ('absent', None)

    if kind == 'link_header':
        nxt = _link_next(headers, pg.get('rel') or 'next')
        if nxt:
            return ('continue', nxt)
        # A Link header that is PRESENT but carries no `next` rel is a POSITIVE
        # terminator, not missing metadata: GitHub keeps sending rel="prev" and
        # rel="first" on the final page. Only a wholly absent header is
        # genuinely unknowable.
        #
        # Conflating the two made a correct walk report complete=False whenever
        # the item count was an exact multiple of page_size — the last page is
        # then FULL, so the short-page fallback could not rescue it either.
        # Found against a real 6-issue repo at per_page=2.
        return ('done', None) if _link_header(headers) else ('absent', None)

    if kind == 'offset':
        seen = (state or 0) + len(page_items)
        total_path = pg.get('total_path')
        if total_path:
            total = get_path(data, total_path)
            if isinstance(total, int):
                return ('done', None) if seen >= total else ('continue', seen)
        # No total: a short page is the terminator, a full page continues.
        # This is a real terminator rather than absent metadata, so offset
        # paging never trips the full-page-no-metadata guard below.
        if len(page_items) < page_size:
            return ('done', None)
        return ('continue', seen)

    raise BoardError(f'unsupported pagination kind {kind!r}')


def fetch_items(connector, http, *, credential='', max_pages=None,
                timeout=DEFAULT_TIMEOUT):
    """Walk the board's list endpoint and return normalized items.

    Always returns a dict with an honest `complete` flag:

        {items, raw_count, complete, truncation_reason, pages_fetched, status}

    `complete` is True only when the vendor positively told us the walk ended.
    Specifically, a FULL page with no pagination metadata yields
    complete=False — that combination is exactly the Jira-truncation and
    GitLab-keyset signature and is never legitimately "done". A connector that
    reported success on a partial board would be worse than no connector.
    """
    lst = connector['list']
    pg = lst['pagination']
    page_size = lst.get('page_size', 50)
    limit = max_pages if max_pages is not None else lst.get(
        'max_pages', schema.DEFAULT_MAX_PAGES)

    base_ctx = {'base_url': connector['base_url'], 'page': {}}
    base_headers = auth_headers(connector, credential)

    raw_items = []
    pages = 0
    complete = False
    truncation_reason = ''
    state = None
    override_url = None
    last_status = 0

    while pages < limit:
        extra_query = None
        body_override = None
        ctx = dict(base_ctx)
        ctx['page'] = {'state': state} if state is not None else {}

        if state is not None and pg['kind'] in ('page_token', 'cursor'):
            slot, path = next(iter(pg['into'].items()))
            if slot == 'query':
                extra_query = {path: state}
            else:
                body_override = json.loads(json.dumps(lst['request'].get('body') or {}))
                set_path(body_override, path, state)
        elif state is not None and pg['kind'] == 'offset':
            extra_query = {pg['offset_param']: state}
            if pg.get('limit_param'):
                extra_query[pg['limit_param']] = page_size

        method, url, headers, body = render_request(
            lst['request'], ctx, base_headers=base_headers,
            extra_query=extra_query, body_override=body_override)

        # next_url / link_header hand back an ABSOLUTE next URL, which replaces
        # the templated one. It is still passed through the same guarded http
        # callable, so a `next` pointing at 169.254.169.254 is refused.
        if override_url:
            url = override_url

        status, resp_headers, resp_body = http(
            url, method=method, headers=headers, body=body, timeout=timeout)
        last_status = status

        if status >= 400:
            # A cursor that expired mid-walk is a known, expected failure: a run
            # cannot store a cursor, pause and resume, so we report the partial
            # result honestly rather than pretending the board is this small.
            if pages > 0 and pg['kind'] == 'cursor':
                truncation_reason = 'cursor_expired'
                break
            return {
                'items': [], 'raw_count': 0, 'complete': False,
                'truncation_reason': f'http_{status}', 'pages_fetched': pages,
                'status': status,
                'error': _error_snippet(resp_body),
            }

        data = _decode(status, resp_body, f'list page {pages + 1}')
        page_items = get_path(data, lst['items_path'])
        if page_items is None:
            return {
                'items': [], 'raw_count': 0, 'complete': False,
                'truncation_reason': 'items_path_not_found',
                'pages_fetched': pages, 'status': status,
                'error': (f'items_path {lst["items_path"]!r} did not resolve; '
                          f'top-level keys were '
                          f'{sorted(data)[:8] if isinstance(data, dict) else type(data).__name__}'),
            }
        if not isinstance(page_items, list):
            raise BoardError(
                f'items_path {lst["items_path"]!r} resolved to '
                f'{type(page_items).__name__}, expected a list')

        raw_items.extend(page_items)
        pages += 1

        verdict, value = _next_page(pg, data, resp_headers, page_items,
                                    page_size, state)
        if verdict == 'continue':
            if pg['kind'] in ('next_url', 'link_header'):
                override_url = value
                state = value
            else:
                override_url = None
                state = value
            continue
        if verdict == 'done':
            complete = True
            break
        # 'absent' — no metadata. A SHORT page is genuinely the end; a FULL one
        # means we cannot tell, and we must not claim we can.
        if len(page_items) < page_size:
            complete = True
        else:
            truncation_reason = 'full_page_no_pagination_metadata'
        break
    else:
        truncation_reason = 'max_pages'

    items, map_errors = [], []
    for raw in raw_items:
        try:
            items.append(map_item(raw, connector))
        except BoardError as e:
            map_errors.append(str(e))

    out = {
        'items': items,
        'raw_count': len(raw_items),
        'complete': complete,
        'truncation_reason': truncation_reason,
        'pages_fetched': pages,
        'status': last_status,
    }
    if map_errors:
        out['map_errors'] = map_errors[:10]
    return out


def _error_snippet(body):
    try:
        return (body or b'')[:300].decode('utf-8', 'replace')
    except Exception:
        return ''


# ── mapping ────────────────────────────────────────────────────────────────

def _resolve_field(spec, raw, ctx):
    """One mapped field: a dotted path, a `template`, or a `join` composite.

    Lenient by construction — see interpolate()'s `strict` note. A mapped field
    is display data, not a request."""
    if isinstance(spec, str):
        return get_path(raw, spec)
    if isinstance(spec, dict):
        if 'template' in spec:
            return interpolate(spec['template'], ctx, strict=False)
        if 'join' in spec:
            parts = [get_path(raw, p) for p in spec['join']]
            present = [str(p) for p in parts if p is not None and p != '']
            if not present:
                return None
            return (spec.get('sep') or '+').join(present)
    return None


def _normalize_enum(field, raw_value, enums):
    """Map a vendor value into the closed set, keeping the vendor's own value.

    An UNMAPPED value normalizes to None and passes through as raw rather than
    being coerced. GitHub is the test case: closed+completed and
    closed+not_planned both normalize to CLOSED and differ only in raw — and
    for a board processor that difference is the whole point.
    """
    if raw_value is None:
        return {'normalized': None, 'raw': None}
    mapping = (enums or {}).get(field) or {}
    for bucket, raws in mapping.items():
        for candidate in raws:
            if str(candidate).lower() == str(raw_value).lower():
                return {'normalized': bucket, 'raw': raw_value}
    return {'normalized': None, 'raw': raw_value}


def _resolve_object(spec, raw, ctx):
    if not isinstance(spec, dict):
        return {}
    out = {}
    for key, sub in spec.items():
        val = _resolve_field(sub, raw, ctx)
        if val is not None:
            out[key] = val
    return out


def map_item(raw, connector):
    """Vendor object → canonical item.

    `raw` is ALWAYS retained in full. The normalized fields are a convenience
    layer for the UI and for agent prompts; they are never the only copy of the
    truth, which is what lets the normalized enums stay as small as they are.
    """
    mp = connector.get('map') or {}
    enums = connector.get('enums') or {}
    ctx = {'base_url': connector['base_url'], 'item': raw, 'raw': raw}

    ident = _resolve_field(mp.get('id'), raw, ctx)
    if ident is None or ident == '':
        raise BoardError(
            f'map.id ({mp.get("id")!r}) did not resolve for an item — '
            f'without a stable global id, processed-markers would collide')

    item = {
        'id': str(ident),
        'key': _as_str(_resolve_field(mp.get('key'), raw, ctx)),
        'ref': _resolve_object(mp.get('ref'), raw, ctx),
        'title': _as_str(_resolve_field(mp.get('title'), raw, ctx)),
        'body': _as_str(_resolve_field(mp.get('body'), raw, ctx)),
        'status': _normalize_enum(
            'status', _resolve_field(mp.get('status_raw'), raw, ctx), enums),
        'priority': _normalize_enum(
            'priority', _resolve_field(mp.get('priority_raw'), raw, ctx), enums),
        'assignee': _resolve_object(mp.get('assignee'), raw, ctx),
        'contact': _resolve_object(mp.get('contact'), raw, ctx),
        'collection': _resolve_object(mp.get('collection'), raw, ctx),
        'tags': _as_tags(_resolve_field(mp.get('tags'), raw, ctx)),
        'url': _as_str(_resolve_field(mp.get('url'), raw, ctx)),
        'created_at': _as_str(_resolve_field(mp.get('created_at'), raw, ctx)),
        'updated_at': _as_str(_resolve_field(mp.get('updated_at'), raw, ctx)),
        'raw': raw,
    }
    return item


def _as_str(v):
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    return str(v)


# Vendors disagree on tag shape: Jira `fields.labels` is a list of plain
# strings, GitHub `labels` a list of objects, Linear's a connection of nodes.
# These are the keys a tag-like object realistically carries, most specific
# first.
_TAG_NAME_KEYS = ('name', 'title', 'key', 'label', 'id')


def _as_tags(v):
    """Normalize a mapped tags value to a list of strings.

    An object element is reduced to its human-readable key rather than
    str()-ed: stringifying the dict put a Python repr
    (`{'id': 11748764427, 'node_id': ...}`) in the UI and in agent prompts as
    if it were a tag name, which is worse than dropping it, because it reads
    as data. Found mapping real GitHub labels.
    """
    if v is None:
        return []
    out = []
    for t in (v if isinstance(v, list) else [v]):
        if t is None:
            continue
        if isinstance(t, dict):
            for key in _TAG_NAME_KEYS:
                val = t.get(key)
                if isinstance(val, (str, int)) and str(val).strip():
                    out.append(str(val))
                    break
            continue
        if isinstance(t, (list, tuple)):
            continue        # nested lists are not a tag shape any vendor uses
        out.append(str(t))
    return out


def content_hash(item):
    """Stable digest of the fields that mean "this item changed".

    Deliberately NOT the whole raw object: vendors churn fields that do not
    represent a change the agent should react to. Keyed on id so an edited item
    becomes re-eligible for processing while an untouched one does not.

    `updated_at` is deliberately EXCLUDED, which is the subtle part. Every
    vendor bumps it on any touch — including our own comment. Hashing it made
    a run invalidate its own processed markers the moment it wrote anything,
    so re-running a board immediately re-selected exactly the items it had
    just worked: the guarantee "the second pass does nothing" was false for
    precisely the items that mattered. Observed against a real GitHub repo —
    approving one staged comment put that issue straight back in the next
    run's selection.

    The cost is that a change confined to fields outside this digest — a new
    customer comment, a label, an assignee — no longer re-opens an item. That
    is a much smaller loss than it sounds: none of those are part of the
    canonical item, so a re-run triggered by one would feed the agent
    byte-identical inputs and produce the same proposal, which the vendor-side
    idempotency marker would then skip anyway.
    """
    payload = json.dumps({
        'id': item.get('id'),
        'title': item.get('title'),
        'body': item.get('body'),
        'status': (item.get('status') or {}).get('raw'),
        'priority': (item.get('priority') or {}).get('raw'),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]


# ── actions ────────────────────────────────────────────────────────────────

def _select(sel, ctx, step_where):
    """Resolve a `select`: find ONE element of a prior step's response whose
    fields match. This is what makes Jira expressible — the set of available
    transitions is status- and permission-dependent, so transition ids are not
    stable and must be looked up per item, per call."""
    source = get_path(ctx, sel['from'])
    if not isinstance(source, list):
        raise BoardError(
            f'{step_where}: select.from {sel["from"]!r} resolved to '
            f'{type(source).__name__}, expected a list')
    wanted = {k: interpolate(v, ctx) for k, v in sel['where'].items()}
    for element in source:
        if all(str(get_path(element, path)).lower() == str(want).lower()
               for path, want in wanted.items()):
            return element
    available = []
    for element in source[:12]:
        first_path = next(iter(wanted))
        available.append(get_path(element, first_path))
    raise BoardError(
        f'{step_where}: select matched nothing for {wanted!r}. '
        f'Available values for {next(iter(wanted))!r}: {available!r}')


def action_hash(board_id, item_id, action_name, params):
    """Stable identity for one intended write, used in comment markers so a
    retry recognises our own prior comment instead of posting twice."""
    payload = json.dumps(
        [board_id, item_id, action_name, params], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def _already_applied(action, ctx, http, base_headers, marker, timeout):
    """Probe the board for our own marker before writing again.

    Returns True when a prior identical comment is found. Probe failures are
    NOT fatal — but they are reported by the caller, because "we could not
    check" must not silently read as "it is safe to post"."""
    idem = action.get('idempotency') or {}
    probe = idem.get('probe')
    if not probe:
        return False, None
    method, url, headers, body = render_request(
        probe, ctx, base_headers=base_headers)
    status, _hdrs, resp_body = http(
        url, method=method, headers=headers, body=body, timeout=timeout)
    if status >= 400:
        return False, f'idempotency probe failed (HTTP {status})'
    data = _decode(status, resp_body, 'idempotency probe')
    entries = get_path(data, idem['probe_items_path']) or []
    if not isinstance(entries, list):
        return False, 'idempotency probe path did not resolve to a list'
    field = idem['probe_field']
    for entry in entries:
        if marker and marker in _as_str(get_path(entry, field)):
            return True, None
    return False, None


def run_action(connector, item, action_name, params, http, *, credential='',
               board_id='', timeout=DEFAULT_TIMEOUT):
    """Execute one NAMED action from the connector's allowlist.

    The allowlist is the safety boundary: an agent invokes a declared action
    with parameters and cannot construct an arbitrary request. Returns

        {ok, action, steps, evidence, skipped, error}

    `ok` reflects what the VENDOR API returned, not what anyone believes
    happened — a disposition of "completed" has to be a checkable claim.
    """
    actions = connector.get('actions') or {}
    action = actions.get(action_name)
    if action is None:
        raise BoardError(
            f'action {action_name!r} is not declared by this connector '
            f'(allowed: {", ".join(schema.action_names(connector)) or "none"})')

    supplied = dict(params or {})
    for pname, pspec in (action.get('params') or {}).items():
        if pspec.get('required') and pname not in supplied:
            raise BoardError(f'action {action_name!r}: missing required '
                             f'parameter {pname!r}')

    base_headers = auth_headers(connector, credential)
    ahash = action_hash(board_id, item.get('id'), action_name, supplied)
    marker = ''
    idem = action.get('idempotency') or {}
    if idem.get('marker_template'):
        marker = interpolate(idem['marker_template'], {
            'board_id': board_id, 'item': item, 'action_hash': ahash})

    ctx = {
        'base_url': connector['base_url'],
        'item': item,
        'params': supplied,
        'marker': marker,
    }

    probe_warning = None
    if idem.get('probe'):
        applied, probe_warning = _already_applied(
            action, ctx, http, base_headers, marker, timeout)
        if applied:
            return {
                'ok': True, 'action': action_name, 'steps': [],
                'skipped': 'already_applied',
                'evidence': {'marker': marker, 'action_hash': ahash},
            }

    step_results = []
    for i, step in enumerate(action['steps']):
        sw = f'action {action_name} step {i + 1}'
        if step.get('select'):
            sel = step['select']
            ctx[sel['as']] = _select(sel, ctx, sw)

        method, url, headers, body = render_request(
            step, ctx, base_headers=base_headers)
        status, resp_headers, resp_body = http(
            url, method=method, headers=headers, body=body, timeout=timeout)

        parsed = None
        if resp_body:
            try:
                parsed = json.loads(resp_body.decode('utf-8', 'replace'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = None

        step_results.append({
            'step': i + 1, 'method': method, 'url': url, 'status': status,
        })

        if status >= 400:
            return {
                'ok': False, 'action': action_name, 'steps': step_results,
                'error': f'{sw} returned HTTP {status}: {_error_snippet(resp_body)}',
                'evidence': {'marker': marker, 'action_hash': ahash,
                             'probe_warning': probe_warning},
            }

        if step.get('id'):
            ctx[step['id']] = parsed

    return {
        'ok': True, 'action': action_name, 'steps': step_results,
        'evidence': {'marker': marker, 'action_hash': ahash,
                     'probe_warning': probe_warning},
    }


def write_cost(connector, action_name):
    """How many units of the per-item write budget this action consumes.
    comment + status + assign burning 3 of Zendesk's 30-per-10-min-per-ticket
    is the case this exists for."""
    action = (connector.get('actions') or {}).get(action_name) or {}
    return int(action.get('writes', 1))
