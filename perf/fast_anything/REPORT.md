# fast-anything on kube-coder: measured findings

**Bottom line.** The one CPU problem that matters is in Python, and it is an
algorithm bug, not a language problem. `page_watch._TextExtractor._emit`
re-sums every collected chunk on every emit, so `page_watch.fingerprint` is
**quadratic in page size**. That costs 1 s per poll on a 53 KB page, 15.8 s on a
213 KB page and 56 s on a 428 KB page. A three-line running counter makes it
linear, with byte-identical output: 28 ms, 123 ms and 230 ms, so **36× to 243×**.

The three Rust ports below are real and verified. They are 7×, 72× and 7× per
call and 1.7× end to end on the workload. But in a live workspace they save
roughly **0.6 s of CPU per busy hour**, about 0.3% of what the dashboard burns
idling.

**Recommendation:** fix `page_watch` in Python (a separate small PR). **Don't**
integrate the Rust ports.

Tool: fast-anything v0.5.0, rustc/cargo 1.99, Python 3.12, PyO3 0.30
(abi3-py310). Shared pod with 2-3 vCPU and other Builds running, so wall-clock
numbers carry the ± that `bench` reports.

---

## 1. Method

`workload.py` simulates a **busy workspace hour** (`--minutes` scales it). It
calls the real kube-coder functions with synthetic inputs, sized from the
code's limits and from real file sizes (section 6). No live state is opened.

The per-hour call counts come from the polling intervals in the code:

| function | calls / busy hour | source of the rate |
|---|---:|---|
| `server.parse_screen_prompt` (50-line pane) | 1,800 | `MessageChat.tsx` `PROMPT_POLL_MS = 4000` plus mission-control cards |
| `server.strip_ansi` on 6 KiB log tails | 1,350 | `MissionDrawer.tsx` `DETAIL_REFRESH_MS = 8000` × 3 Builds; `_MC_TAIL_BYTES = 6144` |
| `server.strip_ansi` on a whole `output.log` (1 MB) | 20 | `get_task_output` / `handlers/tasks.py` fallback once tmux is gone |
| `page_watch.fingerprint` (25 KB and 55 KB HTML, no selector) | 24 | two 5-minute page watches |
| `instruction_scan.scan_text`, 100k-char page | 2 | `PageWatchManager.build_payload` on a page change with `include_content` |
| `instruction_scan.scan_text` + `decode_tag_block`, 4-20 KB `.md` | 30 | `scan_tree` over instruction files |
| `gateway.chunk_text` (12 KB answer) | 30 | gateway replies |
| Prometheus `Exposition` (50 families × 12 = 600 labelled samples) | 60 | PodMonitor default `interval: 60s`; a live scrape here has 611 labelled samples |
| `scans.sort_findings` / `count_severities` / `diff_findings` (150 findings) | 20 | scan views |
| `memory.manager._extract_terms` / `_build_fts_query` / `_rrf_fuse` | 120 | memory-inject hook per prompt |
| `hypervisor_session._expand_choices` | 2,000 | one per assistant event |
| `token_usage.add_usage` | 3,000 | one per usage-bearing transcript record |

The first draft of the model used a 15 s scrape. I corrected it to the
PodMonitor default before porting; commit `perf(fast-anything): match the
Prometheus scrape model…`.

I ran two scans:

1. **As shipped:** `python3 -m workload --minutes 15`.
2. **Quadratic bug patched in-process:** `python3 -m workload --minutes 240 --pw-fixed`.
   It swaps `_TextExtractor._emit` for a version with a running length
   counter, so the profile shows what is hot *after* the real fix. The patch is
   in `workload.py` and never touches product source.

## 2. Candidates considered

| candidate | verdict | why |
|---|---|---|
| `page_watch.fingerprint` / `_TextExtractor` | **the finding** | Runs on every poll over up to 1 MiB (`safe_http.DEFAULT_MAX_BYTES`). Pure-Python `html.parser` plus an O(n²) length check. |
| `instruction_scan.scan_text` | ported | Per-code-point Python loop over up to 200,000 chars (`page_watch.MAX_TEXT_CHARS`). |
| `server.strip_ansi` | ported | Regex substitution over whole `output.log` files (median 1.07 MB, max 8.6 MB here). |
| `prometheus_exposition.render_labels` | ported | Called 600× per scrape, about 3.5 µs each in Python. |
| `server.parse_screen_prompt` | measured, not ported | 6.5% in scan 2, but its input is a 50-line pane, so per call it's tiny. I stopped at three ports. |
| `token_usage.add_usage` | measured, not ported | 3.2% in scan 2; small dicts. |
| `instruction_scan.decode_tag_block` | measured, not ported | 2.1%; it only runs on files that already have findings. |
| `token_usage._scan_file`, `hypervisor_session.parse_claude_session_log` | not portable | They do file I/O, and per-line cost is `json.loads` (already C). `_scan_file` also resumes from a byte offset, so the steady state reads only new lines. |
| `worktrees.status` / `_porcelain` / diff parsing | not portable, not hot | `git` subprocess dominates; the parsing is a `split('\0')` over a few hundred records. |
| `gateway.chunk_text`, `scans.*`, memory text helpers | too small | All under 0.5% of both scans; inputs are a query string or about 150 dicts. |
| memory vector KNN | already native | sqlite-vec `vec0` (`memory/store.py:544`). |
| Go `keeper/` | excluded | HTTP proxy, idle tracker and queue, with no CPU-bound pure functions (route parsing and header filtering only). Its `go.mod` also needs Go 1.26 and the pod has 1.22. |
| `charts/workspace-controller/controller.py` | excluded | Kubernetes API I/O. |

## 3. Scan tables

### Scan 1: as shipped (`--minutes 15`, 28.3 s under the profiler)

```
   #  function                        time      calls  verdict
   1  page_watch.fingerprint         94.3%          6  ✓ port it
   2  prometheus_exposition.render_…  0.5%      9,000  · too small to matter
   3  instruction_scan.scan_text      0.5%          9  · too small to matter
   4  server.strip_ansi               0.3%        343  · too small to matter
   5  server.parse_screen_prompt      0.2%        450  · too small to matter
   6  token_usage.add_usage           0.1%        750  · too small to matter
   7  instruction_scan.decode_tag_b…  0.1%          8  · too small to matter
   8  prometheus_exposition.format_…  0.0%      9,000  · too small to matter
   9  scans.diff_findings             0.0%          5  · too small to matter
  10  scans.sort_findings             0.0%          5  · too small to matter
```

As shipped, nothing except `page_watch` registers at all. A cProfile of one
300-row page puts **97% of the time in the `sum(len(c) for c in self._chunks)`
generator** at `page_watch.py:322`: 10.1M generator steps for 4,500 emits.

### Scan 2: page_watch quadratic patched (`--minutes 240 --pw-fixed`, 17.8 s under the profiler)

```
   #  function                        time      calls  verdict
   1  page_watch.fingerprint         39.1%         96  ✓ port it
   2  prometheus_exposition.render_… 16.0%    144,000  ✓ port it
   3  instruction_scan.scan_text     13.7%        128  ✓ port it
   4  server.strip_ansi               8.2%      5,480  ✓ port it
   5  server.parse_screen_prompt      6.5%      7,200  ✓ port it
   6  token_usage.add_usage           3.2%     12,000  ✓ port it
   7  instruction_scan.decode_tag_b…  2.1%        120  ✓ port it
   8  prometheus_exposition.format_…  1.2%    144,000  · too small to matter
   9  scans.diff_findings             0.3%         80  · too small to matter
  10  scans.severity_of               0.3%     24,000  · too small to matter
```

The raw outputs are in `results/scan1.txt` and `results/scan2.txt`.

## 4. The finding: `page_watch` is quadratic

```python
def _emit(self, text):
    ...
    if sum(len(c) for c in self._chunks) >= MAX_TEXT_CHARS:   # O(chunks) per call
        self._overflow = True
        return
    self._chunks.append(text)
```

Every tag transition emits a `' '` boundary chunk, so the chunk count grows with
tag count and the total work is O(tags²). The fix is to keep a running
`_nchars`. It must be bumped in `_emit` *and* at the sibling-match
`self._chunks.append(' ')` in `handle_starttag`, which bypasses `_emit`. That
gives the identical overflow point. `workload._patch_page_watch_emit` does this
with a list subclass that counts on `append`, and
`page_watch_quadratic.py` checks that `(hash, text)` is byte-identical at every
size.

`python3 perf/fast_anything/page_watch_quadratic.py 50 100 180 400 800 1600 3200`:

| HTML | visible text | shipped | fixed | speedup | output identical |
|---:|---:|---:|---:|---:|:-:|
| 7.4 KB | 1.6 KB | 21.8 ms | 3.9 ms | 6× | ✓ |
| 13.8 KB | 2.9 KB | 81.0 ms | 7.8 ms | 10× | ✓ |
| 24.4 KB | 5.0 KB | 227.5 ms | 12.8 ms | 18× | ✓ |
| 53.3 KB | 10.7 KB | 986 ms | 27.6 ms | 36× | ✓ |
| 105.9 KB | 21.1 KB | 3.84 s | 56.6 ms | 68× | ✓ |
| 212.8 KB | 42.5 KB | 15.76 s | 122.9 ms | 128× | ✓ |
| 427.9 KB | 85.7 KB | **55.96 s** | 230.4 ms | **243×** | ✓ |
| 1,020 KB (fetch cap) | over 200k chars | about 5 min (extrapolated) | 594 ms, then the overflow guard | — | — |

**Why it matters to a user.** `PageWatchManager.check_once` runs synchronously
in the HTTP handler: both the scheduled trigger hit and the dashboard's **Check
now** button go through `handlers/triggers.py:_run_page_watch_check`. With
no selector, an ordinary 200-400 KB page (a GitHub releases page, a status
page) means:

- **Check now** spins for 15-60 s.
- A 5-minute watch on a 428 KB page burns **about 19% of a core continuously**:
  56 s of every 300 s, competing for the GIL with every other dashboard
  request for that whole time.
- Near the 1 MiB fetch cap, a single check outlasts the watch interval.

A selector bounds the cost to the matched subtree, but users won't know that.

This is not a Rust job. After the fix, a Rust `fingerprint` would save roughly
another 100 ms per poll on a 200 KB page. It would also have to reproduce
CPython's `html.parser` (tokeniser quirks, `convert_charrefs` with the full HTML5
entity table, malformed markup) and `_decode`'s charset sniffing, byte for byte,
against verify's derived random byte slices of real pages. I ran `fast-anything
port page_watch.fingerprint`, read the TASK, and deliberately did not write that
port.

## 5. Ports

Each port was verified first by `fast-anything verify`: every recorded call,
plus the edge cases and derived inputs it generates. I then ran a second,
hostile differential, `edge_check.py`. It compares values and, for errors, the
exception type and message. The Rust lives in `perf/fast_anything/ports/<id>/`.

| port | verify | extra edge cases | per call (verify) | e2e bench, cumulative (median of 5) |
|---|---|---|---|---|
| `prometheus_exposition.render_labels` | 500/500 recorded | 328/328 | 3.5 µs → 531 ns (**6.7×**) | 5.75 s → 5.27 s (1.09×, ±352 ms) |
| `instruction_scan.scan_text` | 348/348 (128 recorded + 20 edge + 200 derived) | 484/484 | 5.2 ms → 71.8 µs (**72×**) | 5.70 s → 4.47 s (1.28×, ±143 ms) |
| `server.strip_ansi` | 732/732 (500 recorded + 32 edge + 200 derived) | 621/621 | 176.2 µs → 24.9 µs (**7.1×**) | 6.25 s → 3.66 s (**1.71×**, ±243 ms) |

The Python baseline drifted between 5.70 s and 6.25 s across benches; that is
CPU contention from other Builds on this pod. fast-anything's own report card
is in `results/fast-anything-report.md` and `results/report.svg`.

Notes on fidelity: the bugs these ports avoid.

- **`render_labels`:** `LABEL_NAME_RE.match` uses `$`, which also matches before
  one trailing `\n`, so `"abc\n"` is a *valid* label name in the Python. The
  port reproduces that. Non-dict or non-str-keyed input goes through Python's
  own `sorted()`, so mixed key types raise the same `TypeError`. Errors are
  raised as the module's own `ExpositionError`. Truncation is by code point on
  the raw value, before escaping.
- **`scan_text`:** the excerpt uses Python's `str.strip()` whitespace set,
  which includes U+001C..U+001F; Rust's `char::is_whitespace` doesn't. ZWJ
  emoji joins skip skin-tone and variation selectors, exactly like
  `_zwj_is_emoji_join`. A text with no candidate code point (all of ASCII)
  returns after one scan, which is where the 11,927× pure-ASCII number below
  comes from.
- **`strip_ansi`:** a hand-written scanner rather than a regex crate. It's exact
  because the CSI classes `[0-9;?]`, `[ -/]` and `[@-~]` are disjoint, so greedy
  matching never backtracks. OSC fails without a BEL, and a cached "no BEL after
  here" keeps that linear. Non-str input is handed to Python's `re`, so error
  messages are identical.

### Per call at real-world input sizes (`microbench.py`, Rust extension vs. original)

| input | Python | Rust | speedup | saved per call |
|---|---|---|---|---|
| render_labels × 600 (one scrape) | 3.33 ms | 457.0 µs | 7.3× | 2.87 ms |
| scan_text 2.3 KB instruction file (p50) | 1.03 ms | 17.0 µs | 60.4× | 1.01 ms |
| scan_text 9.9 KB instruction file (p90) | 4.59 ms | 36.7 µs | 125.0× | 4.56 ms |
| scan_text 36 KB instruction file (max) | 11.12 ms | 138.4 µs | 80.3× | 10.98 ms |
| scan_text 36 KB, pure ASCII | 10.17 ms | 0.9 µs | 11,927× | 10.17 ms |
| scan_text 200k-char page (cap) | 57.34 ms | 816.0 µs | 70.3× | 56.52 ms |
| strip_ansi 6 KiB tail (mission control) | 59.4 µs | 8.1 µs | 7.3× | 51.3 µs |
| strip_ansi 1.07 MB output.log (p50) | 11.25 ms | 1.47 ms | 7.7× | 9.78 ms |
| strip_ansi 2.5 MB output.log (p90) | 22.00 ms | 3.27 ms | 6.7× | 18.73 ms |
| strip_ansi 8.6 MB output.log (max) | 79.38 ms | 12.02 ms | 6.6× | 67.35 ms |

## 6. Real-world relevance

Real input sizes come from this workspace, using file sizes only (`find
-printf %s`); no file was opened:

- `output.log` over 246 Builds: p50 1.07 MB, p90 2.5 MB, p99 4.6 MB, max 8.6 MB.
- `CLAUDE.md` / `AGENTS.md` / `.cursorrules` over 51 files: p50 2.3 KB, p90
  9.9 KB, max 36 KB.
- A live `/metrics/prometheus` scrape: 660 lines, 85.8 KB, 611 labelled samples.

CPU saved per **busy** hour, from the per-hour counts times the per-call savings
above:

| port | saving / busy hour |
|---|---:|
| render_labels (60 scrapes × 2.87 ms) | 0.17 s |
| scan_text (30 files × about 4.6 ms + 2 pages × 56.5 ms) | 0.25 s |
| strip_ansi (1,350 tails × 51 µs + 20 whole logs × 9.8 ms) | 0.27 s |
| **total** | **about 0.7 s** |

The end-to-end bench agrees: (6.25 − 3.66) s over 4 simulated hours is about
0.65 s per hour.

For scale, the live dashboard idles at about 5% of a core, which is about 180 s
of CPU an hour. So the three ports together trim about **0.3–0.4%** of the
server's CPU. The worst single-call latency they remove is 67 ms, opening the
output tab of the largest finished Build, and 57 ms, payload-scanning a
maximum-size page change. Neither is perceptible next to the network and tmux
round-trips around them.

The workload's 1.7× is real for the workload, but the workload is pure CPU with
all the I/O taken out. The live server is I/O-bound, and its end-to-end
improvement would be invisible.

By contrast, the `page_watch` fix saves **about 14 s per busy hour** with just two
watches on 25-55 KB pages. That's 20× everything the Rust saves. With one watch
on a 428 KB page it saves about **11 minutes of CPU per hour**, and it turns
"Check now" from a minute-long hang into a quarter of a second.

## 7. Integration cost if the Rust were ever shipped

- **Not a ConfigMap anymore.** Since #430 the Python backend is baked into the
  devlaptop image (`COPY charts/workspace/*.py /opt/browser-src/` in
  `devlaptop/Dockerfile`), so the old "server.py comes from a ConfigMap" blocker
  is gone.
- **Build stage.** Shipping would mean a new `rust:` builder stage in
  `devlaptop/Dockerfile`: `cargo build --release` for three PyO3 crates, then
  copy three `.abi3.so` files (about 430 KB each) into `/opt/browser-src`. The
  runtime image needs no Rust toolchain. A cold build pulls PyO3 and compiles
  with `lto = "fat"`, which adds minutes to every image build unless cached.
- **Swap mechanism.** fast-anything's swap block is appended to the product
  module. In-tree we'd instead want an explicit `try: from _kc_native import …
  except ImportError:` with the Python as the fallback, plus a test that runs
  both.
- **Architecture.** CI builds `platforms: linux/amd64` only today. The abi3
  build covers every CPython ≥ 3.10, but adding arm64 nodes later means a second
  native build, while pure Python runs anywhere unchanged.
- **Maintenance.** Each port is now two implementations of the same contract.
  `instruction_scan` is a security control (#559), and a divergence there is a
  silent false negative. Every future change to the Python, such as a new
  invisible code point or label rule, has to land twice and be differentially
  tested.
- **Dev loop.** `server.py` is run straight from source in dev and tests. Native
  modules add a `cargo build` step before tests are meaningful.

## 8. Recommendation

1. **Integrate (Python, separate PR): fix `page_watch` quadratic text-length
   check.** Bump a running `_nchars` in `_emit` and at the sibling-match append
   in `handle_starttag`. Add a regression test that bounds the cost: for
   example, a 2,000-row page must fingerprint well under a second, or count the
   chunk-length work. The output is byte-identical, verified up to 428 KB
   (`page_watch_quadratic.py`), so stored `last_hash` values don't change and no
   watch fires spuriously. This branch deliberately leaves `charts/` untouched.
2. **Don't integrate the Rust ports.** They are correct and fast per call, but
   they buy about 0.7 s of CPU per busy hour and no perceptible latency, for a
   permanent second implementation, a native build stage and an arch matrix.
3. **Defer:** revisit only if the shape changes, for example a Prometheus
   scrape with tens of thousands of series, or `strip_ansi` on every poll of
   multi-MB logs. Even then, try Python first: `strip_ansi` could avoid
   re-stripping a whole log by tailing it the way mission control already does.

## 9. Reproduce

```bash
export PATH=/home/dev/.cargo/bin:$PATH RUSTUP_HOME=/home/dev/.rustup CARGO_HOME=/home/dev/.cargo
export PYTHONPATH=$PWD/perf/fast_anything          # run from the repo root
F=/home/dev/fast-anything-src/fast-anything/target/release/fast-anything
cd charts/workspace                                 # the import base fast-anything needs (-m keeps cwd as base)
$F scan -- python3 -m workload --minutes 15                 # scan 1
$F scan -- python3 -m workload --minutes 240 --pw-fixed     # scan 2
$F port server.strip_ansi                                   # records calls, creates .fast/ports/<id>/
cp ../../perf/fast_anything/ports/server.strip_ansi/lib.rs .fast/ports/server.strip_ansi/src/lib.rs
$F verify server.strip_ansi && python3 ../../perf/fast_anything/edge_check.py server.strip_ansi
$F enable server.strip_ansi && $F bench && $F disable server.strip_ansi
python3 ../../perf/fast_anything/microbench.py              # after verify has built the .so files
python3 ../../perf/fast_anything/page_watch_quadratic.py    # no Rust needed
```

Two gotchas:

- fast-anything resolves portable modules relative to the **entry script's
  folder**. Running `python3 perf/fast_anything/workload.py` marks every
  kube-coder function "can't be imported from the app's folder". Hence the
  `-m workload` invocation from `charts/workspace`.
- `.fast/` is gitignored. After `disable`, `git diff origin/main -- charts/` is
  empty.

## Files

- `workload.py`, `inputs.py`: the workload driver and its input builders.
- `page_watch_quadratic.py`: shipped vs. fixed `fingerprint`, with an identity
  check.
- `edge_check.py`: hostile differential, Python vs. Rust.
- `microbench.py`: per-call timings at real sizes.
- `ports/<id>/lib.rs` and `Cargo.toml`: the hand-written Rust.
- `results/`: raw scan, micro-benchmark and fast-anything report outputs.
