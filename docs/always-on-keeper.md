# Always-on keeper — design review and staging plan

Design note for **issue #728** ("always-on keeper — tiny Go/Rust server for
webhooks + crons so idle workspaces can sleep").

**Status: nothing in this document is enabled.** No keeper exists, no chart
template references one, and no workspace can sleep. One slice of the proposal
*is* implemented — the workspace-side idle contract, `GET /api/keeper/idle`
(§4.1) — because it is the same whichever way the open questions in §3 are
answered, and because getting it wrong is what kills somebody's running agent.
The rest is written down here rather than built, for the reasons in §3 and §6.

This page is not in `docs/_manifest.json` (the in-app docs nav): there is no
user-facing behaviour to document yet. It belongs there when sleep ships.

---

## 1. What the issue asserts, and what the tree actually says

Every claim #728 makes about today's code was checked. They hold, with the
line references below — which is worth stating plainly, because it means the
proposal is aimed at the right seams.

| Claim in #728 | Verdict | Evidence |
|---|---|---|
| Webhooks land in `server.py`, ingress-routed straight at `ws-<user>:6080` | true | `WebhookManager` — `charts/workspace/server.py:6834`; backend in `charts/workspace/templates/ingress-webhooks.yaml:44-51` |
| Cron and page-watch fires are a CronJob `curl` at `WORKSPACE_INTERNAL_URL` | true | the generated command — `charts/workspace/server.py:10556`; `CronManager:10136`, `PageWatchManager:10588` |
| `holding-<user>` is always up, its own Deployment, and is the ingress custom-error backend | true | `charts/workspace/templates/holding-deployment.yaml`; `ingress.yaml:30-31` and `ingress-oauth2.yaml:23-24` (`custom-http-errors` + `default-backend`) |
| `ingress-claude-api.yaml` (the bearer/mobile path) has **no** custom-error backend | true | no `custom-http-errors` or `default-backend` anywhere in that file — a scaled-to-0 workspace really does give the phone a raw nginx 503 |
| A phone user cannot wake a stopped workspace without an admin token | true | `start`/`stop` is `check_admin()`-gated — `charts/workspace-controller/controller.py:2709-2731` |
| Auth still runs in front of a sleeping workspace, so the waking page is never shown pre-auth | true, and load-bearing | oauth2-proxy is its **own** Deployment (`charts/workspace/templates/oauth2-proxy.yaml:14`), so it survives the workspace going to 0 |

Two further facts the issue does not mention, both in its favour:

- **Scale-to-0 is proven machinery, not new.** The controller already scales
  workspaces to 0 and back for its Stop/Start buttons
  (`scale_workspace`, `controller.py:397`). A keeper doing the same thing is
  reusing a path that works today.
- **The SSH sidecar shares the pod's network namespace**
  (`deployment.yaml:540-564`), so the workspace can see SSH sessions in
  `/proc/net/tcp` — which is what makes the "no open terminal or SSH session"
  idle signal observable from inside the pod at all.

And one against:

- **There is no Go (or Rust) anywhere in this repository.** No `go.mod`, no
  `Cargo.toml`. Everything is Python, Helm and TypeScript. Q1 is therefore not
  a language preference; it adds a third toolchain, a third test runner in
  `ci.yml`, and a second image to build and push in `release.yml`. See §3.1.

---

## 2. Two things in the proposal do not work as written

Both are in §3 of the issue ("Key design points"), and both have to be settled
before keeper code is worth writing, because both change what the chart looks
like.

### 2.1 `kube-coder.io/sleep: keeper` on the Deployment is not durable

The issue's safety property — "a workspace an admin stopped from the controller
stays stopped" — rests on the keeper annotating the Deployment when *it* is the
one that scaled to 0, and only auto-waking annotated workspaces.

A Deployment annotation cannot carry that. The workspace Deployment is a Helm
template, and the repo already documents what happens to live mutations of it:

> Like start/stop, this mutates live state that a later `helm upgrade` would
> reset — durable changes still belong in the workspace's values.yaml.
> — `charts/workspace-controller/controller.py:408-415`

So the first self-serve update, GitOps reconcile or `make deploy` after a
workspace falls asleep strips the annotation. The keeper then sees a
0-replica Deployment with no annotation, which is exactly its encoding of
"an admin stopped this" — and the workspace stays asleep for ever, with
webhooks queueing and crons 202-ing into nothing. The failure is silent and
it points the wrong way.

**Fix:** sleep state belongs to the keeper, not to the object it manages.
Either a `ConfigMap` the keeper owns (created by the keeper, not the chart, so
Helm never reconciles it) or a record on the keeper's own volume. Whatever
Q3 decides for the event queue can hold this too.

### 2.2 Nobody owns `replicas`

`charts/workspace/templates/deployment.yaml:9-11` hard-codes `replicas: 1`
with `strategy: Recreate`. Every reconcile of a sleeping workspace therefore
wakes it — and with GitOps reconciling on a schedule, a workspace could be
woken repeatedly without a user ever asking, which is the cost saving the
whole feature exists for.

This is the same class of problem as 2.1 but it cannot be fixed by moving
state, because the conflict is structural: two writers (Helm and the keeper)
own one field. The options, in the order I would try them:

1. **Omit `replicas` from the template when `keeper.sleep.enabled`.** Helm
   then leaves the field alone on upgrade and the keeper owns it outright.
   Costs: a brand-new release starts at the API default (1, which is what we
   want) but the chart no longer *declares* the desired state, and the
   controller's Stop button and the keeper can now disagree about who last
   set it. Cheapest, and honest about who owns the field.
2. **Give the controller the decision.** The controller reads live replicas
   before reconciling and passes `--set` accordingly. Keeps the chart
   declarative; puts a read-before-write race in the reconcile path.
3. **Keep `replicas: 1` and accept reconcile-wakes.** Defensible only if
   reconciles are rare and idle re-sleep is quick (the keeper puts it back to
   sleep after `idleMinutes`). It turns a correctness question into a cost
   question — worth measuring before dismissing.

**This is the question that blocks the rest**, because option 1 vs 2 changes
`deployment.yaml`, the controller, and the acceptance criterion
"`keeper.enabled=false` renders exactly today's chart".

---

## 3. The open questions, with a recommendation

### 3.1 Go or Rust? → **Neither, in the first PR. Then Go.**

Go for the reasons the issue gives (client-go, small static binary, matches the
ecosystem), and Rust's memory win is not worth a third toolchain for a process
whose budget is 20 MiB either way.

But note what the *first* keeper PR costs regardless of language: a new
language in a repo that has none, a `go test` job in `ci.yml`, a second image
built and pushed by `release.yml`, and a distroless base to keep current.
That is most of the risk of the feature and none of its user-visible value.

If the aim is to ship sleep sooner, the holding pod is already an always-up
nginx with a ConfigMap — it can queue nothing, but it can serve the waking
page and it is already wired in as the custom-error backend. A keeper that
starts as "the holding pod plus one wake call" is a much smaller first step
than "a new Go service with an on-disk queue". Worth deciding deliberately
rather than by default.

### 3.2 One keeper per workspace, or one shared? → **Per workspace.**

Agreed with the issue, and the reason is stronger than cost: the per-workspace
Role can be `resourceNames`-scoped to exactly one Deployment. A shared keeper
needs cross-namespace `deployments/scale`, which is a single component that can
stop every workspace in the cluster. 15 MiB per user is a good price for that.

### 3.3 Queue storage: PVC or emptyDir? → **A PVC, and it also fixes §2.1.**

"Queued events survive a keeper restart" is an acceptance criterion, and
`emptyDir` does not survive a restart. The same volume can hold the sleep-state
record that §2.1 needs, which turns two decisions into one.

### 3.4 Should sleep default to on? → **No, not in the release that adds it.**

`sleep.enabled: false` by default, as the issue proposes. Idle detection is the
part that hurts when it is wrong (see §4.1), and "default off" lets it be
measured on real workspaces via the `idle` endpoint *before* anything scales
anything. One release with the endpoint reporting and nothing acting on it is
cheap insurance.

### 3.5 Mobile auto-wake on open? → **Tap to wake, first.**

The issue leans auto-wake. Auto-wake makes a glance at cached data cost a
4 GiB pod, and worse, it makes the wake unattributable: a backgrounded app
that refreshes on foreground wakes workspaces the user never opened. Ship
tap-to-wake with the pill showing "Sleeping · Tap to wake", measure how often
people tap, and flip the default if they always do.

### 3.6 The question the issue is missing: who owns `replicas`?

See §2.2. This one has to be answered first.

---

## 4. The keeper ↔ workspace contract

### 4.1 `GET /api/keeper/idle` — implemented

Bearer-authenticated, read-only, no side effects. The keeper polls it; the
workspace answers with observations and a verdict, and the keeper keeps the
policy (`keeper.sleep.idleMinutes`) and the scaling.

```
GET /api/keeper/idle?idle_minutes=30
Authorization: Bearer <the workspace's Claude Task API token>

{
  "idle": false,
  "reason": "busy: 1 live task of 12",
  "busy_signals": ["tasks"],
  "idle_for_s": 0.0,
  "quiet_since": 1790645123.4,
  "idle_threshold_s": 1800,
  "idle_minutes": 30,
  "watching_since": 1790640000.0,
  "now": 1790645123.4,
  "signals": {
    "tasks":       {"busy": true,  "known": true, "since": 1790645120.0, "detail": "1 live task of 12"},
    "board_runs":  {"busy": false, "known": true, "since": 1790644000.0, "detail": "0 live runs of 3"},
    "terminals":   {"busy": false, "known": true, "since": null, "detail": "0 terminal sessions"},
    "code_server": {"busy": false, "known": true, "since": null, "detail": "0 editor connections"},
    "dashboard":   {"busy": false, "known": true, "since": null, "detail": "0 dashboard clients"}
  }
}
```

`?idle_minutes=` lets the keeper state the threshold it was configured with
instead of the value having to be kept in step in two charts; the response
echoes what it used. A malformed value falls back to the default rather than
erroring, so a bad question cannot make a workspace un-sleepable.

Implementation: `charts/workspace/keeper_idle.py`, wired at
`charts/workspace/handlers/system.py`, tested in
`charts/workspace/tests/keeper_api_test.py`.

**Where each signal comes from, and why that source:**

| Signal | Source | Note |
|---|---|---|
| `tasks` | `ClaudeTaskManager.list_tasks()` | Live is `running` or `waiting-for-input` — the same pair `_reconcile_status` treats as unfinished (`server.py:4504`). A Build parked on a question is **not** idle; it is a human's turn. |
| `board_runs` | `BoardRunsManager.list_runs()` filtered by `boards.runs.is_live` | Reuses the run-liveness predicate from #712 (`boards/runs.py:368`), which already knows `status == "running"` outlives the work. |
| `terminals` | ESTABLISHED connections to ttyd (7681) and the SSH port | The ssh-server sidecar shares the pod netns, so one `/proc/net/tcp` read sees both. |
| `code_server` | ESTABLISHED connections to 8080 | An open VS Code tab holds a websocket; that is its heartbeat. |
| `dashboard` | ESTABLISHED connections to 6080 from a **non-loopback** peer, minus the caller | See below. |

**Three rules that are the whole point of the module, and are pinned by tests:**

1. **Every ambiguity resolves to "busy".** A signal whose source cannot be read
   reports `known: false` *and* `busy: true`. A broken probe must never read as
   "nobody is here": the cost of a false "idle" is a killed agent, a terminal
   mid-command and an unsaved buffer; the cost of a false "busy" is a pod that
   stays up a while longer.
2. **`quiet_since` never predates this process.** A server that booted 10s ago
   cannot attest to an hour of quiet no matter how empty the disk looks, so the
   answer is clamped to `watching_since`. (A consequence worth knowing: a
   workspace that restarts every 20 minutes can never satisfy a 30-minute
   threshold. That is the correct behaviour, not a bug — but it means "sleep
   never happens" should be diagnosed by reading `watching_since` first.)
3. **The poll does not count as activity.** The keeper is a different pod, so
   its request arrives non-loopback and would otherwise register as a dashboard
   client — every 60s, for ever, on the strength of asking "may I put you to
   sleep?". The endpoint excludes the peer it is answering.

   **This makes one deployment choice mandatory:** the keeper must poll the
   workspace **Service** directly. If its poll is routed back through the
   ingress, its peer address becomes indistinguishable from a browser's and
   the dashboard signal is blinded — every workspace then looks permanently
   busy, or (if the exclusion is widened to that address) permanently idle
   while a user is typing.

Loopback is excluded from the dashboard signal for the same family of reasons:
the kubelet probes, the MCP servers and every in-pod `curl localhost:6080`
would otherwise pin the workspace awake for ever.

### 4.2 Still to build

- `POST /api/keeper/triggers` (workspace → keeper): the **IDs** of webhooks,
  crons and page-watches, pushed on create/delete and at boot. No secrets.
  Unknown ID ⇒ 404 and no wake.
- `POST /api/keeper/token-hash` (workspace → keeper): SHA-256 of the API token,
  so the keeper can reject a bad bearer without holding the credential.
- `GET /api/keeper/status`, `POST /api/keeper/wake` (keeper, for the app).
- Re-applying existing CronJobs at boot when `WORKSPACE_INTERNAL_URL` changed,
  so old manifests pick up the keeper's address.

---

## 5. Staging plan

Ordered so each step is separately reviewable and separately revertible, and so
the two PRs that carry real risk come after the cheap ones.

| # | Scope | Depends on |
|---|---|---|
| 1 | **The idle contract** — `keeper_idle.py`, `GET /api/keeper/idle`, tests. No keeper, no behaviour change. | — (done) |
| 2 | **Decide §2.2 and §3.1.** Land the `replicas` ownership change and the chart's `keeper:` values block with everything defaulting off, so PR 3 has somewhere to plug in. | §2.2 answered |
| 3 | **The keeper itself** — proxy, wake, on-disk queue, RBAC, Deployment, image, CI job. `keeper.enabled=false` by default; `enabled=false` must render byte-identical to today. | 2 |
| 4 | **Trigger-ID and token-hash push** from `server.py`, CronJob re-apply on URL change. Wakes now work for webhooks and crons. | 3 |
| 5 | **Sleep** — the idle loop, the rate limiter, `sleep.enabled=false` by default, the keeper-owned sleep record from §2.1, controller sleeping-vs-stopped. | 4 |
| 6 | **Mobile** — `workspaceState`, `WakeBanner`, the sleeping-503 JSON in `client.ts`, approval-queue flush, push-tap deferral, demo mode. Independent of 5's default. | 3 (needs `status`/`wake`) |

PR 6 is the whole second half of #728 and is a workstream in its own right: 13
files in `mobile/`, its own vitest suite, and a phone to test on. It should be
its own issue.

---

## 6. What cannot be verified from inside a workspace pod

Stated so nobody reads a green test run as more than it is. §7 of #728 has
eleven backend acceptance criteria and eight mobile ones; the E2E list needs:

- a keeper **image** in the registry (a workspace has no push credentials), and
- permission to scale a workspace Deployment and watch it come back, and
- a phone, for the mobile half.

So "the keeper's ServiceAccount cannot scale any other Deployment", "queued
events survive a keeper restart", "≤ 20 MiB RSS when idle" and every mobile
criterion are cluster work, not something a PR check can assert. What *can* be
asserted here, and is: the idle decision table, the `/proc` parsing, the
endpoint's auth gate and route wiring, and (once PR 2 lands) `helm unittest`
on the rendered chart with the keeper on and off.
