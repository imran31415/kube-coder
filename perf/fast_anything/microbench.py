"""Per-call timings at REAL-WORLD input sizes: original Python vs. Rust port.

Run from charts/workspace after the ports are built (`fast-anything verify`):

    python3 ../../perf/fast_anything/microbench.py

Sizes come from this workspace (stat only, no file opened): output.log p50
1.07 MB / p90 2.5 MB / max 8.6 MB over 246 Builds; CLAUDE.md/AGENTS.md p50
2.3 KB / p90 9.9 KB / max 36 KB over 51 files; a live /metrics/prometheus
scrape has 611 labelled samples; page_watch caps page text at 200,000 chars.
"""
import importlib
import os
import random
import sys
import time

os.environ['FAST_ANYTHING_DISABLE'] = '1'  # import the originals
WS = os.getcwd()
sys.path.insert(0, WS)
sys.path.insert(0, os.path.join(WS, '.fast', 'lib'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import inputs  # noqa: E402


def t(fn, arg, budget=0.6):
    fn(arg)
    n, start = 0, time.perf_counter()
    while True:
        fn(arg)
        n += 1
        el = time.perf_counter() - start
        if el > budget and n >= 3:
            return el / n


def fmt(s):
    return f'{s * 1e6:,.1f} µs' if s < 1e-3 else f'{s * 1e3:,.2f} ms'


def row(label, py, rs):
    print(f'| {label} | {fmt(py)} | {fmt(rs)} | {py / rs:,.1f}× | {fmt(py - rs)} |')


def main():
    rng = random.Random(11)
    prom = importlib.import_module('prometheus_exposition')
    isc = importlib.import_module('instruction_scan')
    srv = importlib.import_module('server')
    fl = importlib.import_module('fa_prometheus_exposition_render_labels').render_labels
    fs = importlib.import_module('fa_instruction_scan_scan_text').scan_text
    fa = importlib.import_module('fa_server_strip_ansi').strip_ansi
    print('| input | Python | Rust | per call | saved per call |')
    print('|---|---|---|---|---|')

    d = inputs.build(random.Random(1795313588))
    samples = [lbl for _, _, ss in d['prom_families'] for lbl, _ in ss]

    def scrape(fn):
        return lambda _: [fn(x) for x in samples]
    row(f'render_labels × {len(samples)} (one scrape)', t(scrape(prom.render_labels), None),
        t(scrape(fl), None))

    for label, text in [
        ('scan_text 2.3 KB instruction file (p50)', inputs._prose(rng, 2_300)),
        ('scan_text 9.9 KB instruction file (p90)', inputs._prose(rng, 9_900)),
        ('scan_text 36 KB instruction file (max)', inputs._prose(rng, 36_000)),
        ('scan_text 36 KB, pure ASCII', 'plain ascii text. ' * 2000),
        ('scan_text 200k-char page (cap)', inputs._prose(rng, 200_000, hidden='​')),
    ]:
        assert isc.scan_text(text) == fs(text)
        row(label, t(isc.scan_text, text), t(fs, text))

    for label, size in [('strip_ansi 6 KiB tail (mission control)', 6144),
                        ('strip_ansi 1.07 MB output.log (p50)', 1_094_231),
                        ('strip_ansi 2.5 MB output.log (p90)', 2_529_133),
                        ('strip_ansi 8.6 MB output.log (max)', 8_620_513)]:
        text = inputs._ansi_text(rng, size)
        assert srv.strip_ansi(text) == fa(text)
        row(label, t(srv.strip_ansi, text), t(fa, text))


if __name__ == '__main__':
    main()
