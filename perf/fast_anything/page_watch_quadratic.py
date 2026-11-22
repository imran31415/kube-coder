"""Measure page_watch.fingerprint as shipped vs. with the O(1) length counter,
and check both return byte-identical (hash, text) on every page size.

    python3 perf/fast_anything/page_watch_quadratic.py
"""
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, '..', '..', 'charts', 'workspace')))
sys.path.insert(0, HERE)

import page_watch  # noqa: E402
import inputs  # noqa: E402
import workload  # noqa: E402


def best_of(fn, n=3):
    best = float('inf')
    for _ in range(n):
        t = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t)
    return best, out


def main():
    rng = random.Random(7)
    rows_list = [int(a) for a in sys.argv[1:]] or [50, 100, 200, 400, 800]
    pages = [(r, inputs._html_page(rng, r)) for r in rows_list]
    orig_init, orig_emit = page_watch._TextExtractor.__init__, page_watch._TextExtractor._emit
    shipped = {}
    for rows, body in pages:
        shipped[rows] = best_of(lambda: page_watch.fingerprint(body, None, content_type='text/html'),
                                n=1 if rows >= 400 else 3)
    workload._patch_page_watch_emit()
    print(f'{"rows":>6} {"html KB":>8} {"text KB":>8} {"shipped ms":>11} {"fixed ms":>9} {"speedup":>8} same')
    for rows, body in pages:
        t0, out0 = shipped[rows]
        t1, out1 = best_of(lambda: page_watch.fingerprint(body, None, content_type='text/html'))
        print(f'{rows:>6} {len(body)/1024:>8.1f} {len(out0[1])/1024:>8.1f} {t0*1e3:>11.1f} '
              f'{t1*1e3:>9.1f} {t0/t1:>7.0f}x {out0 == out1}')
    page_watch._TextExtractor.__init__, page_watch._TextExtractor._emit = orig_init, orig_emit


if __name__ == '__main__':
    main()
