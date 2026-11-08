# Issue 710 implementation and release evidence

Upstream PR branch: `feat/issue-710-upstream`, isolated checkout `C:/Agents/kube-coder-710`.
On 2026-10-04, #701 was confirmed merged. Only the #710 publishing commit was
applied to upstream `f5da764`; the upstream PR excludes the old prerequisite
commits. Integration preserves the new HTTP handler mixins and moves publishing
notification registration into `handlers/feed.py`.

The complete-suite evidence below is historical, from the original #701/#710
stack based on `fbf3739`, not a full regression run on the October upstream base.
Focused publishing, notification and task-route tests, mobile typechecking and
dashboard checks are rerun for the upstream PR and reported in its description.

## Local gates

Measured checks on 2026-09-22:

| Check | Result |
| --- | --- |
| Complete workspace Python suite | 3,512 tests run; OK, 9 skipped (Linux/WSL) |
| Publishing, Git, GitHub, summary and notification payload tests | 44 passed on Linux, using real Git and flock |
| Dashboard unit suite | 1,105 passed across 130 files; all 6 publishing component tests passed after the final View PR change |
| Dashboard production build | Passed |
| Mobile typecheck and unit suite | Passed; 217 tests across 18 files |
| Controller Python suite | 119 passed |
| Controller dashboard suite | 34 passed across 7 files |
| Rendered dashboard and native-component phone flows | Passed, including the enabled View PR action after completion |

The targeted Linux gate additionally covers two independent publisher processes,
cross-process exclusion of worktree reuse/writers, a saved human description
winning over concurrent AI generation, and one readiness event across probing
and preparation. GitHub destination tests use real local remote configuration
with stubbed REST/fetch boundaries for fork parents and `release/next` bases.
Notification tests cover duplicate listener/last-response delivery and responses
arriving after listener detachment.

Process termination is exercised after each of five persisted stages
(`validating`, `committing`, `pushing`, `creating_pr`, `published`), as well as
between branch update and index replacement. Recovery verifies one branch
commit, exact remote SHA, unchanged main checkout, and released reservation.
The terminal-result test caught and fixed a reservation that could otherwise
remain after a crash just before cleanup.

- Linux/WSL real-Git tests exercise immutable review, staging preservation, ownership, double execution, divergence, existing commits, interrupted preparation, and process death between branch-ref and index replacement.
- The real HTTP handler tests authentication, read-only mutation refusal, preparation, immutable diff, submission and result status.
- GitHub adapter tests cover remote identity, lost PR-create response, closed PRs, repo mismatch, permissions and rate limits. Network boundaries are stubbed.
- Dashboard component tests cover older servers, save-before-submit, duplicate taps, connection errors, stale Build responses and updates to an existing PR.
- Native notification logic tests cover hydration/navigation ordering and workspace mismatch. These are not physical-device delivery tests.
- `scripts/check-publish-backend.py` runs the complete Python suite under isolated Feed, push, provider-key, trigger and publication roots. It requires Linux, not the Windows flock shim.

## Reproduce the rendered dashboard integration gate

1. On Linux/WSL, from `charts/workspace`, run `python3 -m tests.publish_fixture 7108`.
2. From `charts/workspace/web`, run `npx vite --config scripts/publish-vite.config.ts`.
3. In another terminal there, run `node scripts/check-publish.mjs`.

The test mounts the production review component, drives actual publishing HTTP routes and a real temporary Git/bare remote, saves/loads a draft, reloads during publishing, verifies SHAs and commit/PR counts, then pushes a reviewed update. GitHub and model boundaries are fixtures. Screenshots are generated under `charts/workspace/web/artifacts/publish/`.

The fixture binds loopback and lives under tests. It is not included in the production API or workspace image's top-level Python module copy.

For the native-component browser gate, run `npm run export:web -- --max-workers 1`
and then `node scripts/check-publish.mjs` from `mobile`. On machines where
Playwright cannot find its bundled browser, set `KC_CHROMIUM` to an installed
Chromium executable. This uses the existing Expo mock environment, not the
production HTTP publisher. Its screenshots preserve the app's existing dark
theme; dashboard screenshots use light mode.

`make` is unavailable in this WSL installation. The workspace Python script and
the separately executed dashboard/controller suites cover the commands behind
`test-all-units`; mobile remains a separate gate. No container image or Helm
verification is claimed: Docker Desktop's daemon is unavailable and Helm is not
installed. The image's existing `COPY charts/workspace/*.py` includes all four
new publishing modules, which add no Python dependencies.

Final logs are retained locally under ignored `artifacts/issue-710/`; browser
screenshots are under the respective dashboard/mobile `artifacts/publish/`
directories. The complete Python run is followed by the 44-test targeted rerun
against the final GitHub fixture, which discovers existing PRs just like the
adapter. The final dashboard production build and Expo export both passed.

## Live release gate still required

Do not close #710 based only on the fixtures. Complete the original plan's live matrix against a designated test workspace, throwaway GitHub repository/fork, personal and App authentication, and physical Android/iOS builds. Record device versions, notification cold-start/background/foreground behavior, exact GitHub head/base SHAs, response-loss recovery and server restart evidence.

The implementation session requested those fixtures/devices while continuing local work. No production workspace deployment or external repository publish is implied by passing local tests.
