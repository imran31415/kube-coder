# Contributing to kube-coder

Thanks for wanting to contribute. kube-coder is a Helm chart + Preact SPA +
Python dashboard backend that provisions per-user Kubernetes development
workspaces. This doc covers the workflow we expect for changes.

## Quick start

1. **Fork the repo.** Push branches to your fork, then open a PR against
   `imran31415/kube-coder:main`.
2. **Run the test suite locally** before pushing — see "Running tests" below.
3. **One topic per PR.** Don't mix unrelated changes; split into separate
   PRs so reviewers can land them independently.

## Running tests

The full suite (helm chart unit tests, server.py unittest, SPA vitest)
runs from the repo root via Make:

```bash
make test-all-units      # vitest + python unittest
helm unittest charts/workspace   # helm chart tests (requires helm-unittest plugin)
helm lint charts/workspace -f charts/workspace/tests/test-values.yaml
helm template test-ws charts/workspace -f charts/workspace/tests/test-values.yaml > /dev/null
```

CI runs all of the above on every PR (see `.github/workflows/ci.yml`).

### Frontend (SPA)

```bash
cd charts/workspace/web
yarn install        # we use yarn 1.x — do NOT commit package-lock.json
yarn test           # one-shot vitest
yarn test:watch     # watch mode
yarn build          # type-check + production build
```

### Backend (server.py)

```bash
cd charts/workspace
python3 -m unittest discover -s tests -p '*_test.py' -v
```

## Code style

- **Python**: stdlib only where possible (server.py is intentionally
  dependency-light). Module-level docstrings on every file; function
  docstrings on anything non-obvious. Type hints encouraged but not
  required.
- **TypeScript / Preact**: strict mode, no `any` escapes in production
  code (tests get more leeway). Use `@preact/signals` for shared state;
  reach for component-local `useState` for ephemeral UI state. Polling
  goes through `hooks/usePoll` so visibility + backoff are consistent.
- **Helm**: every template gets a top-of-file comment explaining the
  "why" — what failure mode this template prevents, what the gate is.
  values.yaml is the source of truth for defaults; add `required` guards
  for anything that has no safe default.

## Schema migrations must be rollback-compatible

**A migration must not add a constraint that older code cannot satisfy.**

Workspaces pin their image tag and the console offers "Restart & update", so
rolling an image *back* is a routine operation. Migrations have no down-step:
once a workspace has migrated, a rolled-back pod runs older code against the
newer schema. If the migration tightened the schema, that older code's writes
start failing — in production, for everyone on that workspace, usually
silently until something important doesn't save.

Free to do, any time:

- add a table
- add a column **with a default** (or nullable)
- add a **non-unique** index
- delete/repair data, rewrite a trigger

Requires a decision, not a reflex:

- `CREATE UNIQUE INDEX`, `UNIQUE(...)` on an existing table
- a `NOT NULL` column with no default
- a `CHECK` constraint or a foreign key added by rebuilding a table

For those, either prove older code still writes (see below) or decide
explicitly that the release is not rollback-safe — and say so in the PR and
the release notes, because it becomes a chart/image rollout-ordering
requirement.

Usually there is a third option that is strictly better: **move the invariant
into code.** #598 wanted one queue row per memory. A `UNIQUE(memory_id)` index
would have got it, and would have broken every memory update on a rolled-back
image (`UNIQUE constraint failed: embeddings_pending.memory_id` — demonstrated
on a live workspace). The shipped version uses a non-unique index plus an
update-or-insert helper: same guarantee, plain `INSERT` from older code still
legal, no rollout ordering.

### Proving it

`charts/workspace/tests/rollback_compat.py` does the check. Build a database
at the previous schema version, hand it the statements the previous version
executed, and it runs them again after the migration:

```python
from rollback_compat import assert_rollback_compatible

assert_rollback_compatible(
    db_path,                       # a tempfile; never a live database
    PREVIOUS_VERSION_STATEMENTS,   # [(sql, params_or_callable), ...]
    build_previous=_build_at(3),
    migrate=_apply(_store._migration_004, 4),
)
```

Add a boundary to `tests/memory_rollback_compat_test.py` for every new
migration — `test_the_audit_covers_every_migration_that_exists` fails if you
forget. Include at least one statement that writes to a row the fixture
*already held*: statements that only touch rows they just created never
exercise a per-row constraint, which is exactly how a `UNIQUE` index slips
past a check that looks thorough.

### Two traps, both non-obvious

- **`CREATE INDEX IF NOT EXISTS` is a silent no-op when a UNIQUE index already
  holds the name.** `IF NOT EXISTS` matches on the name, not the shape. So
  shipping the corrected statement does *not* undo a UNIQUE index an
  in-development build already created — it succeeds, changes nothing, and the
  door stays shut. The repair has to `DROP INDEX` first.
- **A repair for that cannot live inside a version-gated migration.** A
  database stuck in the bad state is *already at* the new version, so the gate
  skips the repair forever — and that database is precisely the one needing it.
  Repairs run **unconditionally on open**, as a property re-asserted every
  time, not a one-time step. See `memory/store._ensure_pending_index_shape`.

Both are pinned as executable facts in `TrapTests` in
`tests/memory_rollback_compat_test.py`. They cost #598 three review rounds.

## Commit + PR conventions

- **Commit format**: `<scope>(<area>): one-line description` where
  `<scope>` is `feat` / `fix` / `chore` / `docs` / `refactor` and
  `<area>` is the directory or feature (e.g. `fix(server)`,
  `chore(helm)`). Body explains *why*, not *what* — the diff already
  shows what.
- **PR title**: matches the squash commit you'd want on `main`. Use the
  scope/area prefix too.
- **PR body**: summary + test plan, plus a screenshot for any UI change.

## Security-sensitive changes

If your change touches auth, RBAC, ingress, SSRF gates, or the public-
demo mode, **call it out in the PR body** so reviewers know to look at
the security surfaces. See `SECURITY.md` for the threat model and
hardening defaults.

## Where to start

Good first issues are labeled `good-first-issue` on the issue tracker.
If you want something larger, file an issue describing what you'd like
to build *before* writing code so we can align on the design.
