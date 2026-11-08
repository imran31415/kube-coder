# Hypervisor to Board validation

Validated on 2026-11-02. Requirements remain in
`hypervisor-board-run-handoff.md`. Live-provider/manual PC testing is deferred
at the user's request; this document does not claim live GitHub or LLM acceptance.

## Automated results

- 242 Python tests passed across Board runs, run APIs, review APIs, isolated
  worktrees, MCP and the approval-request state machine.
- One additional regression test passed for passing the Board worker context
  through task creation into the real subprocess launcher.
- 58 targeted dashboard tests passed: permission cards, status refresh/replay,
  read-only controls, exact run links, Board store and runs panel.
- Keeper `go test -race ./...` passed.
- Dashboard production build and mobile TypeScript check passed.
- Headless browser chat-to-Board acceptance passed after the launcher fix.
  Evidence: `tmp-screenshots/board-handoff-1793558635003/results.json`, with
  screenshots and backend logs in the same ignored directory.

The browser test types a message into Hypervisor, invokes the actual MCP
subprocess and request API, displays the approval card, cancels a request,
then sends another message and approves its request. It checks:

1. No vendor reads, workers or runs before approval; cancellation creates no run.
2. Shared bearer credentials and forged identity headers cannot approve or
   use the older run/probe/action routes to bypass approval.
3. Approval starts one run; the worker stages a proposal without tracker writes.
4. The card reports review needed, survives refresh, opens the exact Board run,
   and links back to the originating chat.
5. Repeating the saved decision creates no duplicate run.
6. Restart preserves the run and request without launching another worker.
7. Narrow viewport layouts do not overflow horizontally.

OAuth, the tracker and agent execution use deterministic adapters. Backend
routes, Keeper authentication and durable decisions, file locking, runner,
review staging and browser UI are real. Separate worktree tests exercise the
real task launcher with tmux and vendor I/O stubbed. This distinction matters:
those tests found and fixed a missing worker-context argument that the browser
fixture's deterministic worker did not exercise.

## Reproduce

Run Python tests on Linux/WSL for real `flock` behavior:

```sh
cd charts/workspace
python3 -m unittest tests.boards_worktree_test tests.boards_runs_test tests.boards_runs_api_test tests.boards_review_api_test tests.mcp_dashboard_test tests.board_handoff_test -q
```

Build the existing Keeper binary and dashboard, then run one headless browser:

```sh
mkdir -p tmp-screenshots
go build -C keeper -o ../tmp-screenshots/kc-board-keeper .
cd charts/workspace/web
yarn build
yarn playwright install chromium
node scripts/board-handoff-e2e.mjs
```

On Windows the browser harness starts its Python backend and Keeper in WSL
Ubuntu; build Keeper there as a Linux binary. No Docker cluster or live
credentials are needed. CI runs the same browser acceptance after the web build.

## Deployment prerequisite and remaining acceptance

The feature defaults off. Enable `boardHandoff.enabled` only with Keeper and
GitHub OAuth configured; unsupported authentication fails closed. The private
local OAuth configuration is not present, and no live test issue or tracker
write was made in this testing pass. A real provider conversation, real worker
and approved disposable repository run still need live acceptance before
claiming the full requirements verified in a deployment.
