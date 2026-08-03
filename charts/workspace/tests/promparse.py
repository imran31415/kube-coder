"""A strict Prometheus text-exposition parser, for tests only (#105).

WHY THIS EXISTS. `prometheus_exposition.py` renders the format; asserting on
the strings it produces only proves it is self-consistent. This is written
from the format specification instead — a character scanner that accepts what
the reference Go parser accepts and rejects what it rejects — so a green parse
is independent evidence rather than a restatement of the writer.

It is deliberately STRICTER than "does it look about right":

  * the document must end in exactly one newline
  * `# HELP` / `# TYPE` may appear at most once per family, and not after that
    family's samples
  * label values must use only the three escapes the format defines
    (`\\\\`, `\\"`, `\\n`); any other escape is a parse error, which is what
    makes the escaping tests meaningful
  * a repeated (name, label set) is an error — the reference parser rejects it
    and the whole scrape is dropped
  * `1_0` is not a number (Python's `float()` accepts it; Go's does not)

`assert_valid` adds the conventions this repo enforces on top: every family
carries a type, counters end in `_total`, gauges do not.

Not a general-purpose parser: histograms/summaries parse as plain samples, and
`{` inside a metric name is not tolerated. Enough for what we emit, strict
about everything we emit.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

METRIC_NAME = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*')
LABEL_NAME = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*')
VALID_TYPES = frozenset({'counter', 'gauge', 'histogram', 'summary', 'untyped'})

#: (name, ((label, value), ...)) — a series identity.
Series = Tuple[str, Tuple[Tuple[str, str], ...]]


class ParseError(Exception):
    """The exposition would be rejected by Prometheus."""


class Parsed:
    def __init__(self) -> None:
        self.samples: Dict[Series, float] = {}
        self.types: Dict[str, str] = {}
        self.helps: Dict[str, str] = {}

    # ── lookups tests want ────────────────────────────────────────────────

    def names(self) -> set:
        return {name for name, _ in self.samples}

    def value(self, name: str, **labels) -> float:
        key = (name, tuple(sorted(labels.items())))
        if key not in self.samples:
            have = sorted(f'{n}{dict(ls)}' for n, ls in self.samples if n == name)
            raise AssertionError(
                f'no series {name}{labels or ""}; have: '
                + (', '.join(have) if have else '(none)'))
        return self.samples[key]

    def series(self, name: str) -> List[Tuple[Dict[str, str], float]]:
        return [(dict(labels), value)
                for (n, labels), value in self.samples.items() if n == name]

    def label_values(self, name: str, label: str) -> set:
        return {dict(labels).get(label) for n, labels in self.samples if n == name}


def _skip_spaces(line: str, i: int) -> int:
    while i < len(line) and line[i] in ' \t':
        i += 1
    return i


def _parse_labels(line: str, i: int) -> Tuple[Dict[str, str], int]:
    """Scan `{a="1",b="2"}` starting at the `{`. Returns (labels, next index)."""
    labels: Dict[str, str] = {}
    i += 1  # past '{'
    i = _skip_spaces(line, i)
    if i < len(line) and line[i] == '}':
        return labels, i + 1
    while True:
        i = _skip_spaces(line, i)
        m = LABEL_NAME.match(line, i)
        if not m:
            raise ParseError(f'expected a label name at offset {i}: {line!r}')
        name, i = m.group(), m.end()
        i = _skip_spaces(line, i)
        if i >= len(line) or line[i] != '=':
            raise ParseError(f'expected = after label {name!r}: {line!r}')
        i = _skip_spaces(line, i + 1)
        if i >= len(line) or line[i] != '"':
            raise ParseError(f'label {name!r} value is not quoted: {line!r}')
        i += 1
        buf: List[str] = []
        while True:
            if i >= len(line):
                raise ParseError(f'unterminated label value: {line!r}')
            ch = line[i]
            if ch == '"':
                i += 1
                break
            if ch == '\\':
                if i + 1 >= len(line):
                    raise ParseError(f'dangling escape: {line!r}')
                nxt = line[i + 1]
                if nxt == '\\':
                    buf.append('\\')
                elif nxt == 'n':
                    buf.append('\n')
                elif nxt == '"':
                    buf.append('"')
                else:
                    raise ParseError(
                        f'invalid escape \\{nxt} in label value: {line!r}')
                i += 2
                continue
            buf.append(ch)
            i += 1
        if name in labels:
            raise ParseError(f'duplicate label {name!r}: {line!r}')
        labels[name] = ''.join(buf)
        i = _skip_spaces(line, i)
        if i < len(line) and line[i] == ',':
            i += 1
            i = _skip_spaces(line, i)
            if i < len(line) and line[i] == '}':   # trailing comma is allowed
                return labels, i + 1
            continue
        if i < len(line) and line[i] == '}':
            return labels, i + 1
        raise ParseError(f'expected , or }} at offset {i}: {line!r}')


def _parse_number(token: str, line: str) -> float:
    if '_' in token:
        raise ParseError(f'not a Go float: {token!r} in {line!r}')
    if token == 'NaN':
        return float('nan')
    if token in ('+Inf', 'Inf'):
        return math.inf
    if token == '-Inf':
        return -math.inf
    if token.lower().lstrip('+-') in ('nan', 'inf', 'infinity'):
        raise ParseError(f'non-canonical special value {token!r} in {line!r}')
    try:
        return float(token)
    except ValueError:
        raise ParseError(f'not a number: {token!r} in {line!r}')


def parse(text: str) -> Parsed:
    """Parse an exposition document or raise `ParseError`."""
    out = Parsed()
    if text == '':
        return out
    if not text.endswith('\n'):
        raise ParseError('document does not end with a newline')
    if text.endswith('\n\n'):
        # Legal for the parser, but for us it means a rendering bug; the writer
        # promises exactly one trailing newline.
        raise ParseError('document ends with a blank line')
    sampled: set = set()

    for line in text.split('\n')[:-1]:
        if line == '':
            continue
        if line.startswith('#'):
            parts = line.split(None, 3)
            if len(parts) >= 2 and parts[1] == 'HELP':
                if len(parts) < 3:
                    raise ParseError(f'# HELP without a metric name: {line!r}')
                name = parts[2]
                if not METRIC_NAME.fullmatch(name):
                    raise ParseError(f'# HELP for invalid name {name!r}')
                if name in out.helps:
                    raise ParseError(f'second # HELP for {name!r}')
                if name in sampled:
                    raise ParseError(f'# HELP for {name!r} after its samples')
                out.helps[name] = parts[3] if len(parts) > 3 else ''
                continue
            if len(parts) >= 2 and parts[1] == 'TYPE':
                if len(parts) < 4:
                    raise ParseError(f'malformed # TYPE line: {line!r}')
                name, kind = parts[2], parts[3].strip()
                if not METRIC_NAME.fullmatch(name):
                    raise ParseError(f'# TYPE for invalid name {name!r}')
                if kind not in VALID_TYPES:
                    raise ParseError(f'unknown metric type {kind!r}')
                if name in out.types:
                    raise ParseError(f'second # TYPE for {name!r}')
                if name in sampled:
                    raise ParseError(f'# TYPE for {name!r} after its samples')
                out.types[name] = kind
                continue
            continue  # any other comment

        m = METRIC_NAME.match(line)
        if not m or m.start() != 0:
            raise ParseError(f'line does not start with a metric name: {line!r}')
        name, i = m.group(), m.end()
        labels: Dict[str, str] = {}
        if i < len(line) and line[i] == '{':
            labels, i = _parse_labels(line, i)
        if i >= len(line) or line[i] not in ' \t':
            raise ParseError(f'expected whitespace before the value: {line!r}')
        rest = line[i:].split()
        if not rest or len(rest) > 2:
            raise ParseError(f'expected "<value> [timestamp]": {line!r}')
        value = _parse_number(rest[0], line)
        if len(rest) == 2:
            try:
                int(rest[1])
            except ValueError:
                raise ParseError(f'timestamp is not an integer: {line!r}')
        key: Series = (name, tuple(sorted(labels.items())))
        if key in out.samples:
            raise ParseError(f'duplicate series: {line!r}')
        out.samples[key] = value
        sampled.add(name)
    return out


def assert_valid(text: str, prefix: Optional[str] = None) -> Parsed:
    """Parse, then enforce this repo's conventions on top of the format."""
    parsed = parse(text)
    for name in parsed.names():
        if name not in parsed.types:
            raise ParseError(f'{name!r} has samples but no # TYPE')
        if name not in parsed.helps:
            raise ParseError(f'{name!r} has samples but no # HELP')
        kind = parsed.types[name]
        if kind == 'counter' and not name.endswith('_total'):
            raise ParseError(f'counter {name!r} does not end in _total')
        if kind == 'gauge' and name.endswith('_total'):
            raise ParseError(f'gauge {name!r} ends in _total')
        if prefix and not name.startswith(prefix):
            raise ParseError(f'{name!r} does not use the {prefix!r} prefix')
    return parsed
