"""Page-watch content extraction, normalization and hashing (#681).

WHAT THIS IS. A page-watch trigger fetches a URL on a schedule and fires its
prompt only on the checks where the content actually changed. This module is
the deterministic half of that: bytes in, a stable hash out. It performs no
I/O, imports nothing outside the stdlib, and knows nothing about Kubernetes,
triggers or tasks — which is what makes the interesting behaviour (baseline,
selector scoping, noise suppression) unit-testable without a network or a pod.

The scheduling half lives in server.py's PageWatchManager; the outbound fetch
belongs to safe_http.py. This module is handed the bytes those produce.

WHY A HAND-ROLLED PARSER. The workspace image ships no beautifulsoup4, no
lxml, no html5lib — `html.parser` is the entire HTML toolchain available, and
safe_http set the precedent that this code path stays stdlib-only. So the CSS
selector support here is a deliberately small subset implemented over
HTMLParser's event stream rather than a DOM. The subset is documented in
SUPPORTED_SELECTORS and enforced at *creation* time, so a user learns the
limit while typing the selector rather than silently at 3am when the watch
quietly stops matching.

THE NOISE PROBLEM. Hashing raw HTML fires on every CSRF nonce, render
timestamp and ad slot — the watch cries wolf on every check and gets switched
off. The fix is to reduce the page to its visible words before hashing:
attributes (where nonces and cache-busting ids live) are discarded wholesale,
script/style/template subtrees are suppressed, and whitespace runs collapse.
That is the *only* normalization: heuristics like "ignore anything that looks
like a date" are unpredictable and miserable to test, and the CSS selector
already gives the user a sharper, more honest tool for narrowing a noisy page.

WHY THE HASH CARRIES A VERSION. Changing normalization changes every stored
hash, which would read as "everything changed at once" and fire every watch in
the workspace simultaneously. NORMALIZER_VERSION is mixed into the hash input
AND stored on the record, so the check can recognise a version change and
re-baseline silently instead of firing. Bump it whenever the rules below
change.

SECURITY. The text this module returns is untrusted third-party content that
is about to be shown to an AI agent. `scan_excerpt` runs it through
instruction_scan (the invisible-instruction detector built for exactly this
threat) before any of it reaches a prompt. See the module's callers in
server.py for the rest of the defence — in particular that a page-watch task
never launches with skip-permissions.
"""

import hashlib
import re

# Bump when the extraction/normalization rules change. Mixed into the hash and
# stored on each record; a mismatch re-baselines the watch rather than firing
# a false "changed" on every watch at once.
NORMALIZER_VERSION = 1

# Hard ceilings. The fetch itself is capped by safe_http; these bound what we
# hold in memory and what can reach a prompt.
MAX_TEXT_CHARS = 200_000
MAX_EXCERPT_CHARS = 2_000

# Depth ceiling for the open-element stack. Real pages nest a few dozen deep;
# a pathological or hostile document should not be able to grow this without
# bound. Elements past the ceiling still contribute text, they just stop being
# pushed, so extraction degrades rather than failing.
MAX_STACK_DEPTH = 512

# Subtrees whose text is never part of "the visible page".
#   script/style — code, not content, and the usual home of cache-busting ids
#   template     — inert by definition; the browser renders none of it
SUPPRESSED_TAGS = frozenset({'script', 'style', 'template'})

# HTML void elements: they never have an end tag, so they must not be pushed
# onto the open-element stack or the depth counter runs away on the first
# `<br>` and every subsequent end tag closes the wrong thing.
VOID_TAGS = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
})

SUPPORTED_SELECTORS = (
    'tag, #id, .class, [attr="value"], combinations of those on one element '
    '(div.card#main), and descendant chains separated by spaces '
    '(.build .status). Not supported: > + ~ combinators, :pseudo-classes, '
    'comma-separated selector lists.'
)


class PageWatchError(Exception):
    """Base for every failure this module reports."""


class SelectorSyntaxError(PageWatchError):
    """The selector uses syntax outside the supported subset.

    Raised at parse time, which callers run at *creation* time so the user is
    told while saving the watch rather than by a silent failure hours later.
    """


class SelectorNoMatch(PageWatchError):
    """The selector matched no element in this document.

    Deliberately distinct from "the selected subtree is empty". A selector
    that previously matched and now matches nothing means the page was
    restructured and the watch is broken — reporting that as an empty page
    would hash to a new value and fire a "change" that is really a
    misconfiguration. Callers treat this as a fetch failure: no fire, baseline
    preserved.
    """


class ExtractError(PageWatchError):
    """The body could not be turned into text (e.g. a truncated read)."""


# ── selector parsing ─────────────────────────────────────────────────────

# One compound selector: an optional tag followed by any number of
# #id / .class / [attr=value] qualifiers, all applying to the same element.
_COMPOUND_RE = re.compile(
    r'''^
    (?P<tag>[A-Za-z][-A-Za-z0-9_]*|\*)?
    (?P<quals>
        (?:
            \#[-A-Za-z0-9_]+
          | \.[-A-Za-z0-9_]+
          | \[\s*[-A-Za-z0-9_:]+\s*(?:=\s*(?:"[^"]*"|'[^']*'|[-A-Za-z0-9_./:]+)\s*)?\]
        )*
    )
    $''',
    re.VERBOSE,
)

_QUAL_RE = re.compile(
    r'''
      \#(?P<id>[-A-Za-z0-9_]+)
    | \.(?P<cls>[-A-Za-z0-9_]+)
    | \[\s*(?P<attr>[-A-Za-z0-9_:]+)\s*
        (?:=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[-A-Za-z0-9_./:]+))\s*)?
      \]
    ''',
    re.VERBOSE,
)


class _Compound:
    """Qualifiers that must all hold for a single element."""

    __slots__ = ('tag', 'id', 'classes', 'attrs')

    def __init__(self, tag=None, id=None, classes=None, attrs=None):
        self.tag = tag
        self.id = id
        self.classes = frozenset(classes or ())
        self.attrs = tuple(attrs or ())

    def matches(self, el):
        """`el` is the (tag, attrs_dict) tuple recorded for an open element."""
        tag, attrs = el
        if self.tag and self.tag != '*' and self.tag != tag:
            return False
        if self.id is not None and attrs.get('id') != self.id:
            return False
        if self.classes:
            present = set((attrs.get('class') or '').split())
            if not self.classes <= present:
                return False
        for name, want in self.attrs:
            if name not in attrs:
                return False
            if want is not None and attrs[name] != want:
                return False
        return True


def parse_selector(selector):
    """Parse a selector into a descendant chain of `_Compound`s.

    Raises SelectorSyntaxError for anything outside SUPPORTED_SELECTORS. The
    error names the subset rather than just saying "invalid", because the
    subset is surprising and the user cannot be expected to guess it.
    """
    if selector is None:
        return None
    raw = selector.strip()
    if not raw:
        return None
    if len(raw) > 200:
        raise SelectorSyntaxError('selector is too long (max 200 characters)')
    for bad, why in ((',', 'selector lists'), ('>', 'child combinators'),
                     ('+', 'adjacent-sibling combinators'),
                     ('~', 'general-sibling combinators'),
                     (':', 'pseudo-classes')):
        # ':' is legal inside an [attr] name (xml:lang) but never outside one.
        if bad == ':' and not re.search(r':(?![^\[\]]*\])', raw):
            continue
        if bad in raw:
            raise SelectorSyntaxError(
                f'{why} are not supported. Supported: {SUPPORTED_SELECTORS}')

    chain = []
    for part in raw.split():
        m = _COMPOUND_RE.match(part)
        if not m:
            raise SelectorSyntaxError(
                f'could not parse {part!r}. Supported: {SUPPORTED_SELECTORS}')
        tag = (m.group('tag') or '').lower() or None
        ids, classes, attrs = None, [], []
        for q in _QUAL_RE.finditer(m.group('quals') or ''):
            if q.group('id'):
                ids = q.group('id')
            elif q.group('cls'):
                classes.append(q.group('cls'))
            elif q.group('attr'):
                value = q.group('dq')
                if value is None:
                    value = q.group('sq')
                if value is None:
                    value = q.group('bare')
                attrs.append((q.group('attr').lower(), value))
        if tag is None and ids is None and not classes and not attrs:
            raise SelectorSyntaxError(
                f'could not parse {part!r}. Supported: {SUPPORTED_SELECTORS}')
        chain.append(_Compound(tag, ids, classes, attrs))
    if not chain:
        return None
    return chain


def _chain_matches(stack, chain):
    """Does the element on top of `stack` satisfy the descendant `chain`?

    Greedy right-to-left: the last compound must match the top element, then
    each earlier compound is satisfied by the nearest ancestor that matches.
    For descendant-only chains (the only kind we support) greedy is exact —
    there is no backtracking case where a later choice would have worked.
    """
    if not chain or not stack:
        return False
    if not chain[-1].matches(stack[-1]):
        return False
    ci = len(chain) - 2
    si = len(stack) - 2
    while ci >= 0 and si >= 0:
        if chain[ci].matches(stack[si]):
            ci -= 1
        si -= 1
    return ci < 0


# ── extraction ───────────────────────────────────────────────────────────

_META_CHARSET_RE = re.compile(
    rb'''<meta[^>]+charset\s*=\s*["']?\s*([-A-Za-z0-9_]+)''', re.IGNORECASE)


def _decode(body, content_type=None):
    """Bytes → str, never raising.

    Charset precedence is the Content-Type header, then a `<meta charset>` in
    the first 2 KiB, then UTF-8. Decoding always uses errors='replace': a
    replacement character is *stable* across checks, whereas raising would
    turn a page with one stray byte into a permanently failing watch.
    """
    if body.startswith(b'\xef\xbb\xbf'):
        body = body[3:]
    encodings = []
    if content_type:
        m = re.search(r'charset\s*=\s*["\']?([-A-Za-z0-9_]+)', content_type,
                      re.IGNORECASE)
        if m:
            encodings.append(m.group(1))
    m = _META_CHARSET_RE.search(body[:2048])
    if m:
        try:
            encodings.append(m.group(1).decode('ascii'))
        except UnicodeDecodeError:
            pass
    encodings.append('utf-8')
    for enc in encodings:
        try:
            return body.decode(enc, errors='replace')
        except (LookupError, ValueError):
            continue
    return body.decode('utf-8', errors='replace')


# HTMLParser is imported lazily-ish at module scope but kept out of the public
# surface; subclassing it is an implementation detail of extract_text.
from html.parser import HTMLParser  # noqa: E402


class _TextExtractor(HTMLParser):
    """Collect visible text, optionally only inside a selector's subtree.

    Streaming rather than DOM-building: we keep a stack of open elements for
    ancestor matching and a capture depth for subtree boundaries. That is
    enough for descendant selectors and costs one pass with no tree in memory.
    """

    def __init__(self, chain=None):
        super().__init__(convert_charrefs=True)
        self._chain = chain
        self._stack = []
        self._suppress_depth = 0
        # None when not capturing; otherwise the stack depth the capture
        # started at, so the matching end tag can be recognised.
        self._capture_at = None
        self._chunks = []
        self._matches = 0
        self._overflow = False

    # -- helpers ---------------------------------------------------------
    @property
    def _capturing(self):
        # With no selector the whole document is the subtree.
        return self._chain is None or self._capture_at is not None

    def _emit(self, text):
        if self._suppress_depth or not text:
            return
        if not self._capturing:
            return
        if sum(len(c) for c in self._chunks) >= MAX_TEXT_CHARS:
            self._overflow = True
            return
        self._chunks.append(text)

    def _boundary(self):
        """Mark a word boundary at a tag transition.

        `<h1>Build</h1><p>passing</p>` is two words on screen, and fusing them
        into 'Buildpassing' would both lose that distinction and make excerpts
        unreadable. Every tag transition therefore emits a space; normalize()
        collapses the runs afterwards, so emitting liberally costs nothing.

        This errs toward splitting: inline markup (`<b>pass</b>ing`) also
        splits, which browsers would not do. That is the safe direction — a
        spurious split still hashes stably, whereas a fusion silently merges
        content that really is separate.
        """
        if self._chunks:
            self._emit(' ')

    # -- parser events ---------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self._boundary()
        attrd = {}
        for k, v in attrs:
            # First occurrence wins, matching browser behaviour on dupes.
            attrd.setdefault(k.lower(), v if v is not None else '')
        if tag in VOID_TAGS:
            # No subtree: it can still *match* a selector, but contributes no
            # text, and it must never touch the stack.
            if self._chain is not None and self._capture_at is None:
                if _chain_matches(self._stack + [(tag, attrd)], self._chain):
                    self._matches += 1
            return
        if tag in SUPPRESSED_TAGS:
            self._suppress_depth += 1
        if len(self._stack) < MAX_STACK_DEPTH:
            self._stack.append((tag, attrd))
        if self._chain is not None and self._capture_at is None:
            if _chain_matches(self._stack, self._chain):
                self._capture_at = len(self._stack)
                self._matches += 1
                if self._chunks:
                    # Separate sibling matches so two adjacent subtrees don't
                    # fuse into one word.
                    self._chunks.append(' ')

    def handle_startendtag(self, tag, attrs):
        # `<br/>` — a start and end in one token. Route through the void path.
        tag = tag.lower()
        if tag not in VOID_TAGS:
            attrd = {k.lower(): (v if v is not None else '') for k, v in attrs}
            if self._chain is not None and self._capture_at is None:
                if _chain_matches(self._stack + [(tag, attrd)], self._chain):
                    self._matches += 1
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        # Before any state change: text collected so far ends at this tag.
        # Running first also means a </script> boundary is correctly dropped,
        # since _suppress_depth is still raised at this point.
        self._boundary()
        # Find the nearest matching open tag. Malformed markup (</div> with no
        # <div>) is ignored rather than corrupting the stack.
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                if self._capture_at is not None and self._capture_at > i:
                    self._capture_at = None
                del self._stack[i:]
                break
        else:
            pass
        if tag in SUPPRESSED_TAGS and self._suppress_depth:
            self._suppress_depth -= 1

    def handle_data(self, data):
        self._emit(data)

    # Comments, doctypes and processing instructions are dropped: HTMLParser
    # routes them to handle_comment/handle_decl/handle_pi, none of which we
    # override, so they never reach handle_data.

    def result(self):
        return ''.join(self._chunks), self._matches, self._overflow


def extract_text(body, selector=None, *, content_type=None, truncated=False):
    """Bytes of a page → its visible text, optionally scoped to `selector`.

    `truncated` says the fetch hit its byte ceiling. That is reported as an
    ExtractError rather than hashed, because the cut-off point drifts as
    unrelated content above it changes — hashing a truncated body would fire a
    "change" on almost every check, forever.

    Raises SelectorNoMatch when a selector matched nothing (see that class for
    why it is not the same as "empty").
    """
    if truncated:
        raise ExtractError(
            'response hit the size ceiling and was truncated; '
            'narrow the watch with a CSS selector')
    chain = parse_selector(selector) if selector else None
    text = _decode(body or b'', content_type)
    parser = _TextExtractor(chain)
    try:
        parser.feed(text)
        parser.close()
    except Exception as e:  # HTMLParser is lenient, but never trust that
        raise ExtractError(f'could not parse the page: {e}')
    collected, matches, overflow = parser.result()
    if chain is not None and matches == 0:
        raise SelectorNoMatch(
            f'the selector {selector!r} matched nothing on this page')
    if overflow:
        raise ExtractError(
            'page text exceeded the size ceiling; '
            'narrow the watch with a CSS selector')
    return collected


def normalize(text):
    """Collapse every whitespace run to a single space and strip.

    This is the whole noise-suppression story, by design. `str.split()` with
    no argument splits on any Unicode whitespace including newlines, tabs and
    non-breaking spaces, which is exactly the set that shifts when a page is
    re-templated without its content changing.
    """
    return ' '.join((text or '').split())


def content_hash(text):
    """Stable fingerprint of already-normalized text.

    NORMALIZER_VERSION is mixed in so hashes from different rule-sets can
    never collide or be compared by accident; the NUL separator keeps the
    version from running into the text.
    """
    payload = f'{NORMALIZER_VERSION}\x00{text}'.encode('utf-8')
    return 'sha256:' + hashlib.sha256(payload).hexdigest()


def fingerprint(body, selector=None, *, content_type=None, truncated=False):
    """extract → normalize → hash, the three steps a check always runs together.

    Returns `(hash, normalized_text)`; the text comes back so a caller that
    wants an excerpt does not have to parse the page a second time.
    """
    text = normalize(extract_text(
        body, selector, content_type=content_type, truncated=truncated))
    return content_hash(text), text


# ── prompt-payload safety ────────────────────────────────────────────────

def scan_excerpt(text, *, include_content=False, scanner=None):
    """Decide what, if any, page text may reach the agent's prompt.

    The watched page is third-party content and the prompt is read by an agent
    that acts on it, so this is a trust boundary, not a formatting step.

    Default is metadata-only: `include_content` must be explicitly turned on
    before a single character of the page is quoted. When it is on, the text
    is first run through instruction_scan (injected as `scanner` for
    testability; it is instruction_scan.scan_text in production), which
    detects the zero-width and Unicode-tag characters used to hide
    instructions from a human reviewer while leaving them legible to a model.

    A high-severity finding SUPPRESSES the excerpt rather than stripping it.
    Silently rewriting text an agent is about to read is its own injection
    vector — instruction_scan's own docstring makes that argument — so the
    payload instead carries the fact that something was hidden, and the agent
    is told the page is not being quoted.
    """
    out = {
        'content_included': False,
        'content_suppressed': False,
        'hidden_findings': 0,
    }
    if not include_content:
        return out
    findings = []
    if scanner is not None:
        try:
            findings = scanner(text) or []
        except Exception:
            # A scanner failure must fail closed: no excerpt.
            out['content_suppressed'] = True
            return out
    out['hidden_findings'] = len(findings)
    if any((f.get('severity') == 'high') for f in findings):
        out['content_suppressed'] = True
        return out
    excerpt = text[:MAX_EXCERPT_CHARS]
    out['content_included'] = True
    out['excerpt'] = excerpt
    out['excerpt_truncated'] = len(text) > MAX_EXCERPT_CHARS
    return out


# ── redirect resolution (creation time only) ─────────────────────────────

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def resolve_redirects(url, *, fetch, max_hops=3, allow_internal=False,
                      timeout=12, max_bytes=None):
    """Follow up to `max_hops` redirects, re-validating every hop.

    safe_http refuses redirects by design: a 3xx target is never re-checked,
    so following one blindly hands an attacker a free bounce to 127.0.0.1 or
    the cloud metadata address. This loop is safe because it does not follow
    anything — it makes a *fresh* `fetch` call per hop, and every one of those
    resolves and pins and public-checks the new host from scratch. No change
    to safe_http is needed or wanted.

    Used only when a watch is created, so the user never has to hunt for the
    post-redirect URL themselves. Check time deliberately does not follow: a
    3xx there means the thing they chose to watch moved, which they should be
    told about rather than have silently papered over.

    Returns `(final_url, status, headers, body)`.
    """
    seen = [url]
    current = url
    kwargs = {'timeout': timeout, 'allow_internal': allow_internal}
    if max_bytes is not None:
        kwargs['max_bytes'] = max_bytes
    for _ in range(max_hops + 1):
        status, headers, body = fetch(current, **kwargs)
        if status not in REDIRECT_STATUSES:
            return current, status, headers, body
        location = None
        for k, v in (headers or {}).items():
            if k.lower() == 'location':
                location = v
                break
        if not location:
            raise PageWatchError(
                f'{current} returned {status} with no Location header')
        nxt = _urljoin(current, location)
        if current.lower().startswith('https://') and \
                nxt.lower().startswith('http://'):
            raise PageWatchError(
                f'{current} redirects to an insecure address ({nxt}); '
                'refusing to downgrade from https to http')
        if nxt in seen:
            raise PageWatchError(f'redirect loop: {" -> ".join(seen)} -> {nxt}')
        seen.append(nxt)
        current = nxt
    raise PageWatchError(
        f'more than {max_hops} redirects starting at {seen[0]}; '
        'watch the final address directly')


def _urljoin(base, location):
    """Resolve a possibly-relative Location against the current URL."""
    from urllib.parse import urljoin
    return urljoin(base, location.strip())
