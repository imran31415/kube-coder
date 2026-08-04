"""Prometheus text exposition format (version 0.0.4), hand-rolled (#105).

WHY THIS EXISTS. `server.py` is deliberately stdlib-only and `prometheus_client`
is not a dependency; pulling one in to print a few dozen numbers is a poor
trade. So the format lives here, alone, where it can be tested exhaustively
without booting an HTTP server — the same precedent as `instruction_scan.py`
and `devcontainer.py`. It knows nothing about kube-coder: it takes names,
labels and values and renders bytes. What to measure lives in
`server.PrometheusMetricsCollector`.

WHY THE FORMAT HAS TO BE EXACTLY RIGHT. A scrape is parsed as a whole. One
malformed line — an unescaped quote inside a label value, a repeated `# TYPE`,
a duplicated label set — makes Prometheus reject the ENTIRE response, so every
other metric on the endpoint vanishes with it and the failure surfaces as a
silent gap in a graph rather than an error anyone reads. That asymmetry is why
this module *refuses* to render something Prometheus would reject instead of
emitting it and hoping.

WHAT IT ENFORCES

  * metric and label names match the character classes Prometheus accepts, and
    the reserved `__`-prefixed label namespace is refused
  * exactly one `# HELP` and one `# TYPE` per family, emitted before its
    samples, never repeated — so a family may only be added once
  * no duplicate label set within a family (the reference parser errors on it)
  * label values escape backslash, double-quote and newline — *in that order*,
    because escaping the quote first would then double-escape its own backslash
  * `# HELP` escapes backslash and newline only; a double-quote is literal
    there, and escaping it would corrupt the help text
  * counters end in `_total` and gauges do not. This is a naming convention
    that Prometheus does not police, so it is enforced here: the cost of
    getting it wrong is a `rate()` on something that isn't monotonic, which
    produces plausible, wrong numbers for as long as nobody checks.
  * the output ends with exactly one newline

CONTROL CHARACTERS. The 0.0.4 text format defines exactly three escapes for
label values (`\\\\`, `\\"`, `\\n`) and two for help text (`\\\\`, `\\n`). There is
no `\\r` and no `\\t`, so emitting one would itself be a parse error. Rather
than pass raw C0 bytes through into a line-oriented format, every control
character except tab (legal raw) and newline (escaped) is dropped.

Values render as ints where they are ints and Go-parseable floats otherwise;
`NaN` / `+Inf` / `-Inf` use the spellings the format defines.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, List, Mapping, Optional, Tuple

#: The `Content-Type` a 0.0.4 exposition must be served with. Prometheus falls
#: back to this format when the header is absent, but stating it keeps other
#: consumers (and humans with curl) from guessing.
CONTENT_TYPE = 'text/plain; version=0.0.4; charset=utf-8'

COUNTER = 'counter'
GAUGE = 'gauge'
_TYPES = (COUNTER, GAUGE)

METRIC_NAME_RE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*$')
LABEL_NAME_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

#: Label values are bounded so one pathological value (a model id read out of
#: an untrusted transcript, say) cannot inflate every scrape forever.
MAX_LABEL_VALUE_LEN = 200

#: C0 controls minus tab (\x09, legal raw inside a quoted value) and newline
#: (\x0a, escaped below), plus DEL.
_CONTROL_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


class ExpositionError(ValueError):
    """Raised for anything that would produce an unparseable exposition."""


def escape_label_value(value: Any) -> str:
    """Escape a label value for the text format.

    Backslash first: doing the quote first would turn `"` into `\\"` and the
    backslash pass would then double it to `\\\\"`, which unescapes to a literal
    backslash followed by an unquoted quote — i.e. the end of the value in the
    wrong place.

    Truncation happens on the RAW value, before escaping. Truncating escaped
    text can cut a `\\\\` pair in half and leave a dangling escape, which is the
    same whole-scrape failure this module exists to prevent.
    """
    s = '' if value is None else str(value)
    s = _CONTROL_RE.sub('', s)
    if len(s) > MAX_LABEL_VALUE_LEN:
        s = s[:MAX_LABEL_VALUE_LEN - 1] + '…'
    return (s.replace('\\', '\\\\')
             .replace('"', '\\"')
             .replace('\n', '\\n'))


def escape_help(text: Any) -> str:
    """Escape help text: backslash and newline only.

    A double-quote is an ordinary character in a `# HELP` line — escaping it
    would put a literal backslash into the rendered help string.
    """
    s = '' if text is None else str(text)
    s = _CONTROL_RE.sub('', s)
    return s.replace('\\', '\\\\').replace('\n', '\\n')


def format_value(value: Any) -> str:
    """Render a sample value the way the text format expects."""
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, int):
        return str(value)
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ExpositionError(f'sample value is not numeric: {value!r}')
    if math.isnan(f):
        return 'NaN'
    if math.isinf(f):
        return '+Inf' if f > 0 else '-Inf'
    return repr(f)


def render_labels(labels: Optional[Mapping[str, Any]]) -> str:
    """`{a="1",b="2"}` for a label mapping, or `''` when there are none.

    Labels are emitted in name order so two scrapes of identical state produce
    identical bytes — which is what makes the output diffable in a test.
    """
    if not labels:
        return ''
    parts = []
    for name in sorted(labels):
        if not isinstance(name, str) or not LABEL_NAME_RE.match(name):
            raise ExpositionError(f'invalid label name: {name!r}')
        if name.startswith('__'):
            raise ExpositionError(f'reserved label name: {name!r}')
        parts.append(f'{name}="{escape_label_value(labels[name])}"')
    return '{' + ','.join(parts) + '}'


class Exposition:
    """An accumulating exposition document. Add families, then `render()`."""

    def __init__(self) -> None:
        # (name, type, escaped help, [(rendered labels, rendered value)])
        self._families: List[Tuple[str, str, str, List[Tuple[str, str]]]] = []
        self._names: set = set()

    def add(self, name: str, metric_type: str, help_text: str,
            samples: Iterable[Tuple[Optional[Mapping[str, Any]], Any]]) -> 'Exposition':
        """Append one metric family. Raises `ExpositionError` on anything
        Prometheus would reject; a family may be added at most once."""
        if not isinstance(name, str) or not METRIC_NAME_RE.match(name):
            raise ExpositionError(f'invalid metric name: {name!r}')
        if metric_type not in _TYPES:
            raise ExpositionError(f'unsupported metric type: {metric_type!r}')
        if metric_type == COUNTER and not name.endswith('_total'):
            raise ExpositionError(f"counter {name!r} must end in '_total'")
        if metric_type == GAUGE and name.endswith('_total'):
            raise ExpositionError(
                f"gauge {name!r} must not end in '_total' — that suffix means "
                'a monotonic counter, and rate() on a gauge lies')
        if name in self._names:
            raise ExpositionError(f'metric family {name!r} added twice')

        rendered: List[Tuple[str, str]] = []
        seen: set = set()
        for labels, value in samples:
            key = render_labels(labels)
            if key in seen:
                raise ExpositionError(
                    f'duplicate label set for {name!r}: {key or "{}"}')
            seen.add(key)
            rendered.append((key, format_value(value)))
        rendered.sort()

        self._names.add(name)
        self._families.append((name, metric_type, escape_help(help_text), rendered))
        return self

    def gauge(self, name: str, help_text: str,
              samples: Iterable[Tuple[Optional[Mapping[str, Any]], Any]]) -> 'Exposition':
        """A value that can go both ways: a level, a depth, a count of things
        that currently exist."""
        return self.add(name, GAUGE, help_text, samples)

    def counter(self, name: str, help_text: str,
                samples: Iterable[Tuple[Optional[Mapping[str, Any]], Any]]) -> 'Exposition':
        """A value that only ever increases within a process lifetime. A
        restart resetting it to 0 is expected and handled by Prometheus; a
        value that can *decrease while the process runs* is not a counter."""
        return self.add(name, COUNTER, help_text, samples)

    def render(self) -> str:
        """The complete document, ending in exactly one newline (or `''` when
        nothing was added — an empty body is a valid, empty scrape)."""
        out: List[str] = []
        for name, metric_type, help_text, samples in self._families:
            out.append(f'# HELP {name} {help_text}')
            out.append(f'# TYPE {name} {metric_type}')
            for labels, value in samples:
                out.append(f'{name}{labels} {value}')
        return ('\n'.join(out) + '\n') if out else ''
