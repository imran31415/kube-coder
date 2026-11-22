"""CPU workload for fast-anything: a simulated busy kube-coder workspace hour.

Every hot function called here is the REAL kube-coder function, imported from
charts/workspace. The driver only builds synthetic inputs and calls them; no
live state is read (nothing under /home/dev/.claude-tasks, .claude-memory or
.credentials is opened).

Per-hour call counts come from the polling intervals in the code
(see REPORT.md, "Workload model"):

  parse_screen_prompt   MessageChat PROMPT_POLL_MS=4s + mission-control cards
  strip_ansi            mission-control 6 KiB log tails (DETAIL_REFRESH_MS=8s),
                        plus the occasional whole-output.log fallback
  page_watch.fingerprint  every page-watch poll, full fetched body
  instruction_scan      page-change payloads (include_content) + scan_tree
  chunk_text            gateway (WhatsApp) replies
  prometheus            /metrics/prometheus scrapes every 15 s
  scans                 findings views
  memory helpers        per-prompt memory injection hook
  hypervisor events     choice-fence expansion per assistant event
  token_usage           per usage-bearing transcript record

Usage:
  python3 perf/fast_anything/workload.py [--minutes M] [--pw-fixed] [--no-page-watch]

--pw-fixed swaps page_watch._TextExtractor._emit for an O(1) running-length
version IN THIS PROCESS ONLY (product source is untouched), so the second scan
shows what is hot once the quadratic text-length check is out of the way.
"""
import argparse
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.normpath(os.path.join(HERE, '..', '..', 'charts', 'workspace'))
sys.path.insert(0, WORKSPACE)

import gateway  # noqa: E402
import hypervisor_session  # noqa: E402
import instruction_scan  # noqa: E402
import page_watch  # noqa: E402
import prometheus_exposition as prom  # noqa: E402
import scans  # noqa: E402
import server  # noqa: E402
import token_usage  # noqa: E402
from memory import manager as mem  # noqa: E402

import inputs  # noqa: E402  (perf/fast_anything/inputs.py — data builders)

# Calls per simulated busy hour (one user, three live Builds, mission control
# open, two page watches on full pages, Prometheus scraping).
PER_HOUR = {
    'parse_screen_prompt': 1800,
    'strip_ansi_tail': 1350,
    'strip_ansi_full_log': 20,
    'page_watch_fingerprint': 24,
    'scan_text_page': 2,
    'scan_text_instruction_file': 30,
    'decode_tag_block': 30,
    'chunk_text': 30,
    'prometheus_render': 240,
    'scans_views': 20,
    'memory_query': 120,
    'expand_choices': 2000,
    'token_usage_add': 3000,
}


class _CountingList(list):
    """A list that keeps the running total of len(item). `_TextExtractor`
    appends to `_chunks` from two places (`_emit` and the sibling-match `' '`
    in handle_starttag), so counting on append keeps the total exact."""
    __slots__ = ('nchars',)

    def __init__(self):
        super().__init__()
        self.nchars = 0

    def append(self, item):
        super().append(item)
        self.nchars += len(item)


def _patch_page_watch_emit():
    """O(1) replacement for the O(n) `sum(len(c) for c in self._chunks)` check
    in page_watch._TextExtractor._emit. Same observable behaviour: overflow
    trips at exactly the same chunk. In-process only; source is untouched."""
    cls = page_watch._TextExtractor
    orig_init = cls.__init__

    def __init__(self, chain=None):
        orig_init(self, chain)
        self._chunks = _CountingList()

    def _emit(self, text):
        if self._suppress_depth or not text:
            return
        if not self._capturing:
            return
        if self._chunks.nchars >= page_watch.MAX_TEXT_CHARS:
            self._overflow = True
            return
        self._chunks.append(text)

    cls.__init__ = __init__
    cls._emit = _emit


def counts_for(minutes):
    """Scale the per-hour model to `minutes` of simulated time (>=1 call each)."""
    return {k: max(1, round(v * minutes / 60)) for k, v in PER_HOUR.items()}


def run(minutes, *, page_watch_on=True):
    rng = random.Random(1795313588)
    d = inputs.build(rng)
    n = counts_for(minutes)
    sink = 0
    for _ in range(n['parse_screen_prompt']):
        r = server.parse_screen_prompt(d['pane'])
        sink += 1 if r else 0
    for i in range(n['strip_ansi_tail']):
        sink += len(server.strip_ansi(d['log_tails'][i % len(d['log_tails'])]))
    for _ in range(n['strip_ansi_full_log']):
        sink += len(server.strip_ansi(d['full_log']))
    if page_watch_on:
        for i in range(n['page_watch_fingerprint']):
            h, _t = page_watch.fingerprint(
                d['html_pages'][i % len(d['html_pages'])], None,
                content_type='text/html; charset=utf-8')
            sink += len(h)
    for _ in range(n['scan_text_page']):
        sink += len(instruction_scan.scan_text(d['page_text']))
    for i in range(n['scan_text_instruction_file']):
        sink += len(instruction_scan.scan_text(d['md_files'][i % len(d['md_files'])]))
    for i in range(n['decode_tag_block']):
        sink += len(instruction_scan.decode_tag_block(d['md_files'][i % len(d['md_files'])]))
    for _ in range(n['chunk_text']):
        sink += len(gateway.chunk_text(d['long_answer']))
    for _ in range(n['prometheus_render']):
        exp = prom.Exposition()
        for name, help_text, samples in d['prom_families']:
            exp.gauge(name, help_text, samples)
        sink += len(exp.render())
    for _ in range(n['scans_views']):
        ordered = scans.sort_findings(d['findings'])
        sink += scans.count_severities(ordered)['total']
        added, changed = scans.diff_findings(d['findings_prev'], d['findings'])
        sink += len(added) + len(changed)
    for i in range(n['memory_query']):
        q = d['queries'][i % len(d['queries'])]
        sink += len(mem._extract_terms(q))
        sink += len(mem._build_fts_query(q))
        sink += len(mem._rrf_fuse(d['rank_a'], d['rank_b']))
    for i in range(n['expand_choices']):
        sink += len(hypervisor_session._expand_choices(d['events'][i % len(d['events'])]))
    total = token_usage.empty_usage()
    for i in range(n['token_usage_add']):
        token_usage.add_usage(total, d['usage_records'][i % len(d['usage_records'])])
    sink += total['records']
    return sink


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--minutes', type=float, default=15.0)
    ap.add_argument('--pw-fixed', action='store_true')
    ap.add_argument('--no-page-watch', action='store_true')
    a = ap.parse_args(argv)
    if a.pw_fixed:
        _patch_page_watch_emit()
    t = time.perf_counter()
    sink = run(a.minutes, page_watch_on=not a.no_page_watch)
    print(f'minutes={a.minutes:g} pw_fixed={a.pw_fixed} sink={sink} '
          f'elapsed={time.perf_counter() - t:.2f}s', file=sys.stderr)


if __name__ == '__main__':
    main()
