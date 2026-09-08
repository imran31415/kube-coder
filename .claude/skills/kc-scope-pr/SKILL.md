---
name: kc-scope-pr
description: Scope a kube-coder change before you open or update a PR — what the diff actually touches, which tests reach it, what the change made worse, and what is left untested. Use after the code is written and before kc-preflight/kc-ship-pr, so the PR description and the tests you run come from the call graph rather than from memory. Also the answer to "is this diff bigger than I think?" and "which tests should I run for this?".
user-invocable: true
allowed-tools: Bash, Read
argument-hint: "[paths…] (default: the current git diff)"
---

# Scope a PR with ripwire

Run **after** the change is written, **before** `kc-preflight` and `kc-ship-pr`.

`ripwire` is baked into the image (`/usr/local/bin/ripwire`, pinned in
`devlaptop/Dockerfile`). Zero deps, and the whole workspace chart — 522 files,
10k symbols — maps in under two seconds, so this is cheap enough to run on a
one-file change.

## Why this exists

This repo has repeatedly shipped changes whose blast radius was larger than the
diff looked. Two from recent history, both of which this skill's output would
have named up front:

* A run-preflight guard was added to `BoardRunsManager.create` and **four**
  board test suites created runs, not one. CI went red with 64 failures and 29
  errors while the same tests passed locally.
* `mcp_dashboard.py` and `server.py` disagreed about one constant. Nothing in
  either file's diff said the other existed.

The point is not the numbers, it is the direction: `--test-gate` answers "which
tests reach what I touched" from the call graph, which is the question a human
answers from memory and gets wrong.

## The three calls

Scope is one root at a time. `charts/workspace` is the usual one; use
`charts/workspace-controller` for controller work.

```bash
ROOT=charts/workspace

# 1. What does this change actually reach, and which tests cover it?
#    Defaults to the git diff. exit 4 = there are tests to run or untested
#    blast radius; that is an obligation, not a failure.
ripwire "$ROOT" --test-gate

# 2. What did I make WORSE? exit 2 only on a pre-existing symbol regressing.
#    A `new-symbol` row never gates — it is still your debt, so read it.
ripwire "$ROOT" --quality-delta

# 3. Did I change a contract someone depends on? (per symbol, ~ms)
ripwire "$ROOT" --edit-check=SomeChangedSymbol
```

`--situ` is the same information as `--test-gate` without the gate, for
mid-task orientation rather than a pre-PR check.

## Reading the output

It is minified XML with a long self-describing header comment — the header
explains every attribute, so read it once rather than guessing. The attributes
that carry the decision:

| attribute | meaning |
|---|---|
| `changed=` | files in the diff |
| `impacted=` | symbols transitively reaching them — the real blast radius |
| `tests=` | test files that reach the change; each `<t>` may carry `run="<cmd>"` |
| `untested=` | impacted symbols no test reaches — `<u>` rows, worst first by `ccx` |
| `gating=` on quality-delta | regressions that will fail the gate |

`counts_floor="1"` means every count is a **floor, not a total**: dynamic
dispatch and script-invoked harnesses are not call edges. `untested=0` is
therefore "nothing the graph can see", never "fully covered".

## What to do with it

1. **Run the tests it names**, not the ones you remember. If `tests=` lists a
   suite you did not expect, that is the finding — go look at why it reaches
   your change before assuming the tool is wrong.
2. **Put `impacted=` in the PR body** when it is large. A reviewer seeing
   "3 files changed" and a four-figure blast radius reads the diff differently.
3. **Fix or justify each `--quality-delta` row.** Do not game the number; a
   wrong abstraction beats a good score.
4. **Treat `untested=` rows as the test-writing list**, highest `ccx` first.

Then hand off: `kc-preflight` runs the suites for real, `kc-ship-pr` opens the PR.

## Honest limits

* **A floor, never a total.** Reflection, dynamic dispatch, and the shell
  harnesses under `tests/` are invisible to a static call graph. `--test-gate`
  reports `script_gates_unmodelled=` for exactly this reason. It narrows what
  you must think about; it does not replace thinking.
* **It cannot see non-code coupling.** Two constants that must agree across two
  files, a Helm value a Python default mirrors, an API shape the SPA assumes —
  no call edge, no edge in the graph. That is precisely the class of bug in the
  second example above, and this skill would not have caught it either.
* **Green here is not green in CI.** It reasons about the tree you have. Run
  `kc-preflight`.
