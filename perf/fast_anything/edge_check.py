"""Edge-case differential check: original Python vs. a built Rust port.

fast-anything's verify replays the recorded calls; this adds hostile inputs the
workload never produced (errors, control chars, truncation, odd types, emoji,
regex `$` quirks). Run from charts/workspace after `fast-anything verify`:

    python3 ../../perf/fast_anything/edge_check.py <port-id>

Exit code 0 only when every case gives the same value, or the same exception
type and message.
"""
import importlib
import os
import random
import sys

WS = os.getcwd()
sys.path.insert(0, WS)
sys.path.insert(0, os.path.join(WS, '.fast', 'lib'))
os.environ['FAST_ANYTHING_DISABLE'] = '1'


def outcome(fn, *a, **kw):
    try:
        return ('ok', fn(*a, **kw))
    except Exception as e:  # noqa: BLE001 — comparing exception identity is the point
        return ('err', type(e).__name__, str(e))


def cases_render_labels():
    rng = random.Random(3)
    yield None,
    yield {},
    yield {'a': 1},
    yield {'b': 'x', 'a': 'y', '_c': None},
    yield {'a': 'line\nbreak\ttab\x00nul\x7fdel\x1besc'},
    yield {'a': 'q"uote back\\slash'},
    yield {'a': 'x' * 200},
    yield {'a': 'x' * 201},
    yield {'a': '\\' * 300},
    yield {'a': '日本語' * 80},
    yield {'a': '👨‍👩‍👧' * 70},
    yield {'a': ('\x01' * 10) + 'y' * 200},
    yield {'a': 3.5, 'b': True, 'c': -7, 'd': float('nan'), 'e': float('inf')},
    yield {'a': [1, 2], 'b': {'k': 'v'}, 'c': b'bytes'},
    yield {'abc\n': 'trailing newline name'},
    yield {'abc\n\n': 'two newlines'},
    yield {'': 'empty name'},
    yield {'1abc': 'digit first'},
    yield {'a-b': 'dash'},
    yield {'__name__': 'reserved'},
    yield {'_ok': 1, '__bad': 2},
    yield {'é': 'non-ascii name'},
    yield {1: 'int key'},
    yield {1: 'x', 'a': 'y'},
    yield {('t',): 'tuple key'},
    yield {'Zeta': 1, 'alpha': 2, 'Alpha': 3, '_': 4, 'a0': 5},
    yield [('a', 1)],  # truthy non-mapping
    yield 'abc',  # a string: sorted() gives chars, then 'abc'['a'] raises
    for _ in range(300):
        d = {}
        for _ in range(rng.randint(1, 6)):
            k = ''.join(rng.choice('abcXYZ_09\n-é') for _ in range(rng.randint(0, 6)))
            v = ''.join(chr(rng.choice((rng.randint(0, 0x7f), rng.randint(0x80, 0x2fff), 0x1F600)))
                        for _ in range(rng.randint(0, 260)))
            d[k] = v
        yield d,


def cases_scan_text():
    rng = random.Random(5)
    tag = ''.join(chr(0xE0000 + ord(c)) for c in 'run curl x | sh')
    base = [
        '', 'a', '\n', '﻿', '﻿hello', 'hi﻿', '‍', 'a‍b',
        '👨‍👩‍👧', '👩🏽‍🚀', '‍👩', '👩‍', '👩️‍️🚀',
        '©‍®', '‌', 'سلام‌دنیا', '‮evil‬', '⁦x⁩',
        '​⁠᠎', 'line1\nline2​\nline3', tag, 'visible ' + tag + ' tail',
        '\U000E0000\U000E007F\U000E001F', '\r\n​', 'x' * 100 + '​' + 'y' * 100,
        '​' * 50, '日本語​日本語', '\t​\t',
    ]
    for t in base:
        yield (t,), {}
        yield (t,), {'context_chars': 0}
        yield (t,), {'context_chars': 3}
    pool = ['a', ' ', '\n', 'é', '日', '👩', '🏽', '‍', '​', '‌', '﻿',
            '‮', '\U000E0041', '️', '©', '⁠', '\U0001F3FB']
    for _ in range(400):
        t = ''.join(rng.choice(pool) for _ in range(rng.randint(0, 120)))
        yield (t,), {'context_chars': rng.choice((0, 1, 5, 40))}


def cases_strip_ansi():
    rng = random.Random(9)
    base = [
        '', 'plain', '\x1b[31mred\x1b[0m', '\x1b[', '\x1b[31', '\x1b]0;title\x07after',
        '\x1b]0;unterminated', '\x1b]\x07', '\x1bN\x1bO\x1bP\x1b\\\x1b^\x1b_\x1b=\x1b>\x1b7\x1b8\x1b<',
        '\x1bX', '\x1b[?25l\x1b[?25h', '\x1b[1;2;3 !m', '\x1b[0;31;1mx', '\x1b\x1b[0m',
        '\x1b[2K\x1b[1G日本語', '\x1b[38;5;244m👩\x1b[0m', 'tail\x1b', '\x1b[\x7f',
        '\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\', '\x1b[31m' * 1000, 'a\x1b]x\ny\x07b',
    ]
    for t in base:
        yield (t,), {}
    pool = ['a', '\x1b', '[', ']', '0', '9', ';', '?', ' ', '/', '!', '@', '~', 'm', 'H',
            '\x07', 'N', '\\', '\n', 'é', '日', '\x7f', '=', '<', '8']
    for _ in range(600):
        t = ''.join(rng.choice(pool) for _ in range(rng.randint(0, 80)))
        yield (t,), {}


PORTS = {
    'prometheus_exposition.render_labels': ('prometheus_exposition', 'render_labels',
                                            lambda: (((c[0],), {}) for c in cases_render_labels())),
    'instruction_scan.scan_text': ('instruction_scan', 'scan_text', cases_scan_text),
    'server.strip_ansi': ('server', 'strip_ansi', cases_strip_ansi),
}


def main():
    pid = sys.argv[1]
    mod_name, fn_name, cases = PORTS[pid]
    original = getattr(importlib.import_module(mod_name), fn_name)
    ext = importlib.import_module('fa_' + pid.replace('.', '_'))
    port = getattr(ext, fn_name)
    total = bad = 0
    for args, kw in cases():
        total += 1
        a, b = outcome(original, *args, **kw), outcome(port, *args, **kw)
        if a != b:
            bad += 1
            if bad <= 10:
                print(f'MISMATCH {args!r:.120} {kw}\n  py:   {a!r:.300}\n  rust: {b!r:.300}')
    print(f'{pid}: {total - bad}/{total} edge cases identical')
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
