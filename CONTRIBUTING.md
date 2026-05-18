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
