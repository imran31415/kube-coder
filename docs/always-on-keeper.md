# Always-on keeper — design review and staging plan

Design note for **issue #728** ("always-on keeper — tiny Go/Rust server for
webhooks + crons so idle workspaces can sleep").

**Status: built through step 4a, all off by default.** Implemented: the
workspace-side idle contract, `GET /api/keeper/idle` (§4.1, step 1); the chart's
`keeper:` values block with the `replicas` ownership change (§2.2, step 2); the
keeper itself — a page that also wakes a workspace it put to sleep, when a
logged-in user visits (§4.3, step 3); and the two things the workspace tells the
keeper, plus the phone's way in (§4.4, step 4a). Nothing puts a workspace to
sleep on its own yet: until step 5's idle loop, that is an operator's
`keeper sleep`. The webhook and cron proxy with its queue (step 4b) and the idle
loop (step 5) are written down here rather than built.

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
| `holding-<user>` is always up, its own Deployment, and is the ingress custom-error backend | true | `charts/workspace/templates/holding-deployment.yaml`; `ingress.yaml:35-36` and `ingress-oauth2.yaml:28-29` (`custom-http-errors` + `default-backend`; with `keeper.enabled` the keeper takes this role, §4.3) |
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
  (Step 3 paid that cost once, as small as it goes: `keeper/` is Go with the
  standard library only — no `go.sum`, no client-go.)

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

**What step 3 did instead:** neither — the API server already keeps the
record. With sleep on, the chart does not declare `replicas` (§2.2), so the
Deployment's `managedFields` say who owns that field, and the keeper writes it
by server-side apply under its own field manager. "Asleep" is "at 0, and the
keeper is the field's only owner"; an admin's Stop takes the field over. It
survives Helm 3 and Helm 4 upgrades and keeper restarts, and needs no storage.
§4.3 has the rule, the measurements, and its one gap.

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

**Decided in the #728 thread: option 1 (landed as step 2).**
`templates/deployment.yaml` renders `replicas: 1` unless
`keeper.enabled && keeper.sleep.enabled`, so with sleep off the chart is byte
for byte what it was. `keeper.sleep.enabled` without `keeper.enabled` fails the
render — sleep with nothing to wake the workspace is never what anyone meant.
Two consequences are documented next to the flag in `values.yaml`, where an
operator will be looking when they happen:

- **Turning sleep on wakes a sleeping workspace once** (Helm 3, which the
  provisioner and CI run). The upgrade that drops the field from the chart
  makes Helm 3's three-way merge delete it, and the API server defaults it back
  to 1. Later upgrades leave it alone. Helm 4 applies server-side: `kubectl
  scale` already took ownership of the field, so dropping it from the chart
  removes nothing and that first upgrade does not wake.
- **Turning sleep off hands the field back to the chart**, so that upgrade also
  sets 1 (both Helm versions).

Checked on a real cluster (minikube, Kubernetes v1.35.1, `base-infrastructure`
then the workspace chart, stop/start via `kubectl scale` — the call
`scale_workspace` makes). Each row: stop the workspace, then `helm upgrade`:

| Chart | Helm 3.21.4 | Helm 4.2.3 |
|---|---|---|
| Before this change | wakes | wakes |
| Sleep off (default) | wakes — unchanged | — |
| Upgrade that turns sleep on | wakes once | stays at 0 |
| Sleep on, three upgrades (incl. a pod-template change) | stays at 0 | stays at 0 |
| Upgrade that turns sleep off | wakes | wakes |

A fresh install with sleep on starts at 1, and a Start after a sleep-on Stop
survives the next upgrade.

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

See §2.2. This one had to be answered first — answered: option 1, the chart
stops declaring `replicas` when sleep is on.

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

Steps 4a and 4b are built; see 4.4 and 4.5. What is left is **step 5, the idle
loop** — the thing that actually puts a workspace to sleep. Until it lands,
sleep is an operator typing:

```bash
kubectl -n <ns> exec deploy/keeper-<user> -- /keeper sleep
```

Everything else about a sleeping workspace now works: a visit wakes it, the app
wakes it, and a webhook or a schedule that fires while it is away is kept and
replayed. Step 5 also closes 4.3's `0 -> 0` gap, by teaching the controller to
show "asleep" as distinct from "stopped".

Two findings from building 4a shaped 4b, both measured in the tree rather than
assumed, and both are why 4b looks the way it does:

- **Slack and Stripe signatures expire; GitHub's does not.**
  `TIMESTAMP_TOLERANCE = 300` is enforced for Slack (`server.py:7180`) *and*
  Stripe (`server.py:7217`), so a request queued across a 60-second wake can
  verify for `github`/`generic` and cannot for the other two — and it fails as
  `404 Not found or unauthorized` with ledger `reason='bad_signature'`, which
  reads as "your secret is wrong". That is why 4.4's push carries each webhook's
  `provider`, and why 4b expires those two inside their own window with a log
  line that names the real cause.
- **Double delivery is not safe in general.** `_ReplayCache` is in-memory only
  (`server.py:6670`) with a 300-second TTL, so it catches a duplicate inside one
  pod lifetime and nothing more — and across a sleep, which is exactly this
  case, it is empty. `handle_cron_fire` has no dedup at all
  (`handlers/triggers.py:428`). So 4b chooses semantics per kind rather than
  inheriting them: webhooks at-least-once, cron and page-watch at-most-once.

### 4.3 The keeper — implemented (step 3)

`keeper.enabled=true` (with `keeper.image`) runs `keeper-<user>`: one static Go
binary on distroless, standard library only — a 4 MB image that idles at about
2 MiB. It **replaces `holding-<user>`** as the workspace ingresses'
custom-error backend and serves the same page, so a Recreate restart still
shows "restarting…". What it adds is what the page does when the workspace is
asleep:

```
browser ──> ingress ws-<user> / ws-<user>-oauth2    (dashboard; auth as today)
              │ workspace has no Ready pod (502/503/504)
              ▼
            Service keeper-<user>        ──> :8080  the page, every path, 503.
                                                    Acts on nothing.
page JS ──> ingress keeper-<user>        (basic auth, or oauth2-proxy)
              │ /api/keeper/status, /api/keeper/wake
              │ (oauth2 mode: /oauth/api/keeper/…)
              ▼
            Service keeper-<user>-wake   ──> :8081  status, and wake (0 -> 1)
```

**Why two Services.** In oauth2 mode the root ingress carries no auth, and the
keeper is its default backend, so `:8080` must never act whatever it is sent.
The wake listener sits behind a Service that only the authenticated keeper
ingress routes to; the default-backend Service does not expose that port at
all, so which port ingress-nginx picks for a default backend never matters
(it is not the first one — measured).

**Wake guards**, each pinned by a test:

- the keeper ingress's auth — a signed-out oauth2 visitor gets the page with a
  "Sign in" link, and every keeper call 302s to sign-in;
- `POST` with `X-Keeper-Wake: 1` — a header a cross-site form can't send, and a
  cross-site fetch can't send without a CORS preflight the keeper never grants;
- only a page someone is looking at (`visibilityState === 'visible'`) asks — a
  tab restored in the background does not;
- never inside another site's frame: every keeper response carries
  `X-Frame-Options: SAMEORIGIN` and `frame-ancestors 'self'`, so a page a
  signed-in user happens to visit can't show (and so wake) their workspace in
  an iframe. Same-origin frames stay allowed, since the ingress serves this page
  in place of the dashboard's own framed tools while the workspace is away;
- `Sec-Fetch-Site`, where the browser sends it, must be `same-origin` — a
  sibling workspace's subdomain is same-*site* but not same-origin;
- exact paths, checked **by the keeper**: on a host where any other Ingress
  uses a regex or `rewrite-target` (both auth modes), ingress-nginx renders the
  keeper ingress's `Exact` paths as case-insensitive *prefix* regexes, so
  `/api/keeper/wake/` and `/API/KEEPER/WAKE` reach the keeper (still through
  auth) and are 404'd there.

**Asleep vs stopped — the rule.** The keeper wakes only a workspace it put to
sleep itself; one an admin stopped stays stopped. "Asleep" is: `spec.replicas`
is 0 **and** the Deployment's `managedFields` name `kube-coder-keeper` as the
only owner of `spec.replicas`. The keeper writes replicas by server-side apply
on the `/scale` subresource (`force`, with the `resourceVersion` it decided on
as a precondition, so a Stop that lands in between wins with a 409 and the
decision is re-made). Measured on Kubernetes 1.35.1:

| Write to `spec.replicas` | Owner afterwards |
|---|---|
| keeper: *merge patch* on `/scale` (the obvious way) | **nobody** — an update via `/scale` that sets 0 records no owner, so it can't tell sleep from Stop |
| keeper: *apply* on `/scale` | `kube-coder-keeper` (Apply, scale) |
| deployment-controller status writes | unchanged |
| `helm upgrade` while asleep, pod template changed — Helm 3.21.4 and 4.2.3 | unchanged (the chart omits `replicas`) |
| admin `kubectl scale` 1 -> 0 or 0 -> 1 (the controller's Stop/Start) | keeper loses it |
| admin `kubectl scale` 0 -> 0 | unchanged (see the gap below) |

Neither a keeper-side copy of the `resourceVersion` nor of the `generation`
could carry this: status writes move the first seconds after every scale, and a
release bump moves the second, so both would mark every sleeping workspace
"stopped" — silently, and in the direction that never wakes it.

**RBAC.** `keeper-<user>`'s Role is `get` on `deployments` and `patch` on
`deployments/scale`, both `resourceNames: [ws-<user>]`. The scale subresource
can change replicas but not the pod template, so a compromised keeper can stop
or start its workspace but not run code in it. A NetworkPolicy admits only the
ingress-nginx namespace, on 8080/8081.

**Putting it to sleep** is by hand until step 5:

```
kubectl -n <ns> exec deploy/keeper-<user> -- /keeper sleep    # -> asleep
kubectl -n <ns> exec deploy/keeper-<user> -- /keeper status   # awake | starting | asleep | stopped
```

`sleep` is exactly as forceful as the controller's Stop — running agents are
killed — and it refuses two things: a workspace an admin stopped (turning that
into "asleep" would let the next visit undo the Stop), and running at all
unless `keeper.sleep.enabled`, because otherwise the chart still sets
`replicas` and the next upgrade wakes it.

**Known gap.** A Stop on a workspace that is *already asleep* is 0 -> 0, which
changes nothing, so the keeper still owns the field and the next visit wakes
it. The controller cannot do this — it offers Stop only on a running
workspace — so only a hand-typed `kubectl scale --replicas=0` can. Step 5's
controller sleeping-vs-stopped view is the place to close it.

#### Running it

**Turning it on** — the workspace must use `ingress.auth.type` `basic` or
`oauth2` (the chart refuses `none`: the keeper wakes for anyone who passes the
ingress auth):

```yaml
keeper:
  enabled: true
  image: ghcr.io/imran31415/kube-coder/keeper:<version>@sha256:<digest>
  sleep:
    enabled: true   # only needed to put it to sleep; see below
```

The upgrade swaps `holding-<user>` for `keeper-<user>` as the ingresses'
default backend; nothing else about the workspace changes.

**Checking it** — the keeper's first log lines say whether it can do its job:

```
$ kubectl -n <ns> logs deploy/keeper-<user>
keeper 1.72.0: looking after ws-<user>/ws-<user>; page :8080, wake :8081
startup check: ws-<user>/ws-<user> is awake
```

Every wake, and every wake refused because an admin stopped the workspace, is
logged with who asked — the signed-in user in oauth2 mode (from the header the
keeper ingress sets from oauth2-proxy's answer, never one a client sent) and
the client address:

```
wake: ws-<user>/ws-<user> scaled 0 -> 1 for user=alice client=203.0.113.9
wake: ws-<user>/ws-<user> is stopped by an admin; not woken for user=alice client=203.0.113.9
```

**When something is wrong:**

| What you see | Why | What to do |
|---|---|---|
| `startup check: cannot read …: 403` in the log; status and wake answer 502 | the Role or RoleBinding `keeper-<user>` is missing or edited | re-run the upgrade; `kubectl -n <ns> auth can-i get deployment/ws-<user> --as system:serviceaccount:<ns>:keeper-<user>` |
| `startup check: cannot read …: dial tcp … i/o timeout` | something blocks the keeper pod's egress to the API server (a cluster-wide default-deny egress policy, say) | allow egress from `app: keeper-<user>` to the API server — the chart sets no egress policy for the keeper |
| the page says "Your workspace is stopped" | `spec.replicas` is 0 and the keeper isn't its only owner: an admin stopped it | Start it from the controller. To see the owners: `kubectl -n <ns> get deploy ws-<user> --show-managed-fields -o json` and look for `f:replicas` |
| a sleeping workspace woke with nobody visiting | an upgrade or GitOps sync with `keeper.sleep.enabled=false` declares `replicas: 1` again | keep `keeper.sleep.enabled=true` in every values file that deploys this workspace |
| `keeper sleep` says `keeper.sleep.enabled is off` | without it the chart still sets `replicas`, so the next upgrade would wake it | set `keeper.sleep.enabled=true` |
| the page says "Your workspace isn't running" with a "Sign in" link | the browser isn't signed in to this workspace (oauth2-proxy answered the keeper call with a sign-in redirect) | sign in; a visit after that wakes it |

**Turning it off** — wake the workspace first (visit it, or Start it), then
upgrade with `keeper.enabled=false` and `keeper.sleep.enabled=false` (the chart
refuses sleep without a keeper, since nothing would wake the workspace). The
chart declares `replicas: 1` again, `holding-<user>` comes back, and
`keeper-<user>` and its RBAC are removed. Waking it first matters for a release
that Helm 4 manages by server-side apply (one it installed; the controller's
provisioner runs Helm 3, which just wakes the workspace): while asleep, the
keeper owns `spec.replicas` at 0, and Helm applying `replicas: 1` over it is a
field conflict — `conflict with "kube-coder-keeper" with subresource "scale":
.spec.replicas` — that fails the upgrade. Awake, both agree on 1, so there is
nothing to conflict over. If it already failed: wake it, and run the upgrade
again. (Measured: Helm 4.2.3 server-side install, sleep, upgrade while asleep
→ still asleep; turn sleep off while asleep → that conflict; wake, then turn it
off → deployed. Helm 3.21.4, same steps → no conflict, replicas back to 1.)

**Not in step 3:** the idle loop and its rate limit (step 5), webhook and cron
wakes and any queueing (step 4), and the phone — its `/api/keeper/wake` still
reaches the bare bearer ingress and gets the plain 503 it falls back to the
controller on, as before.

**Verified on minikube** (Kubernetes 1.35.1, Calico, ingress-nginx, the real
chart and image; `keeper.sleep.enabled=true`):

- basic auth, 64 checks: the RBAC above with the keeper's own token (and
  `can-i`); sleep → visit → Ready in about a minute → the page hands back to the
  live workspace; every unauthenticated, header-less, `GET`, encoded, suffixed,
  cross-site (`Sec-Fetch-Site`) and default-backend variant refused with nothing
  woken; the frame and content-type headers on the page and on the JSON,
  through the ingress; the startup check and the wake line in the log, with no
  user in basic mode; an admin Stop stays stopped; Helm 3 and Helm 4 upgrades
  while asleep leave it asleep and wakeable; a restarted keeper still knows;
  the NetworkPolicy blocks the workspace pod and another namespace;
  `kubectl top` 2 MiB.
- a browser (Chromium, 360 px and 1280 px), 13 checks: a visible tab wakes it
  with one `POST`, a hidden tab sends none, a stopped workspace says so and
  starts nothing.
- clickjacking: a page on another origin frames the workspace while the browser
  holds its basic-auth credentials. With this keeper Chromium refuses to render
  the frame and nothing is called. The same test against a build without the
  frame headers renders the page and wakes the workspace, so the check can
  fail.
- oauth2, signed out, 13 checks: the root page shows "Sign in" and can't be
  framed, every keeper call 302s, a forged identity header does not pass,
  nothing wakes, and the phone's bearer path still answers the plain 503. The
  `/oauth/api/keeper/*` routes were checked in the generated nginx config to go
  to the wake Service ahead of the dashboard's `/oauth/(api/.*)`.
- oauth2 in a browser, 14 checks: signed out, "Sign in" and no keeper call
  answered; signed in through real GitHub and oauth2-proxy, the visit wakes it
  with one `POST` and hands over to the live workspace at `/oauth/`.
  The wake carried a forged `X-Auth-Request-User: mallory`, and the log named
  the real GitHub user: the keeper ingress overwrites that header from
  oauth2-proxy's answer.

---

### 4.4 The two pushes and the phone — implemented (step 4a)

The keeper now knows two things it cannot find out for itself, and the phone can
wake a sleeping workspace. Everything stays behind `keeper.enabled`, and with it
off the chart renders byte-identical to step 3 in both auth modes.

**One listener per class of caller.** Step 3 had two; step 4a adds two more, and
the chart's NetworkPolicy admits each from exactly one place:

| Port | Service | Reached from | Authenticated by |
|---|---|---|---|
| 8080 | `keeper-<user>` | anyone, through the ingress | nothing — it never acts |
| 8081 | `keeper-<user>-wake` | the ingress controller | the ingress (basic auth / oauth2-proxy) |
| 8082 | `keeper-<user>-api` | the ingress controller | the keeper, against the token hash |
| 8083 | `keeper-<user>-control` | the workspace pod only | the keeper, against a shared secret |

**What the workspace pushes** (`charts/workspace/keeper_push.py`):

- `POST /api/keeper/triggers` — the ids of every webhook, cron and page-watch,
  each with its kind and, for webhooks, its provider name. **The whole set every
  time, never a delta**: a delete that raced a keeper restart would otherwise
  leave a deleted id behind, and a deleted webhook that still wakes the
  workspace is the failure this design exists to prevent.
- `POST /api/keeper/token-hash` — `hashlib.sha256(token).hexdigest()`. Enough to
  check the bearer the app presents, not enough to mint one.

Both go at every boot, not only when something changed, because while the
workspace slept the keeper may have restarted, been deployed for the first time,
or lost its store — and nothing else can repair that. Both are also fired on
create and delete, and the token hash on rotation (the one place the token
changes, `handle_claude_regenerate_token`). They run in daemon threads: a keeper
that is down must never turn into a failed save, and a lost push is repaired by
the next boot.

**No secret can leave**, and that is enforced by shape rather than by vigilance.
The projection reads ids from *filenames* and opens a config only to read
`provider`, which is checked against an allowlist. `tests/keeper_push_test.py`
asserts it twice: by name, hunting every secret field the three managers write;
and structurally, requiring every pushed entry's key set to be a subset of
`{kind, id, provider}` — which fails for a field added to a trigger config next
year that nobody thought to add to the first list.

**Why a shared secret as well as the NetworkPolicy.** A `NetworkPolicy` does
nothing whatsoever unless the cluster's CNI implements it — minikube's default
accepts every policy and enforces none, silently. A port that can be made to
hand out wake authority must not have the CNI as its only lock. The secret is
generated by the chart and preserved across `helm upgrade` by a `lookup`, the
same way the controller's admin token is.

**The keeper's own volume.** `keeper-<user>-state`, ReadWriteOnce, 1Gi. It
exists because the facts above can only be pushed while the workspace is awake,
so an in-memory store would come back empty after a restart during a sleep and
every webhook would answer 404 for ever. Two consequences that are easy to miss
and both cost a broken deploy: the Deployment must use `strategy: Recreate`, or
a rolling update deadlocks waiting for a volume the old pod still holds; and the
pod needs `fsGroup: 65532`, or the keeper — non-root with a read-only root
filesystem — cannot write the one directory it is allowed to write. Unlike
`pvc.yaml` it carries no `helm.sh/resource-policy: keep`: everything on it is
re-pushed at the next boot, so keeping it past the release would only orphan
volumes.

**The phone's path.** `POST /api/keeper/wake` and `GET /api/keeper/status` on
`keeper-ingress-api.yaml`, oauth2 mode only — that is the only mode in which the
phone's bearer ingress exists at all. Three things about it are deliberate and
each would be a bug the other way:

- **It carries no ingress auth.** oauth2-proxy would answer a bearer request
  with a 302 the app cannot follow. The keeper checks the token hash itself.
- **It requires neither `X-Keeper-Wake` nor a same-origin `Sec-Fetch-Site`.**
  Those defend a *cookie*-authenticated browser against another site acting for
  the user; a bearer token is never attached automatically, so there is nothing
  to forge, and requiring either would break the shipped app, which sends
  neither.
- **An admin-stopped workspace answers 409, not 200.** The browser's handler
  answers 200 with the state in the body and lets the page read it; the app
  parses no state field and reads any 2xx as "it is coming up", so a 200 here
  would leave the banner saying "Starting your workspace…" for ever about a pod
  that will never start. Not 404 or 405 either — the app reads those as "no
  keeper here" and goes to the admin controller instead of showing the reason.
  The error strings on this listener are therefore user-facing copy, because the
  app renders `{"error": …}` verbatim in the banner.

**Routing, and why it is not left to luck.** On this host every nginx location
renders as a regex, because several Ingresses here use `rewrite-target` — so
`pathType: Exact` is widened to a case-insensitive prefix and the winner is
settled by emission order. Step 3's keeper paths happened to be the longer
string and won; the app's `/api/keeper/wake` is **shorter** than the bearer
ingress's pattern, so relying on the same luck would be a coin toss. Instead the
two location sets are made disjoint: `ingress-claude-api.yaml` excludes exactly
those two paths with a negative lookahead,
`/(api/(?!keeper/(?:status|wake)$).*|metrics|health)`, anchored with `$` so that
step 1's `/api/keeper/idle` still reaches the workspace. Ordering then cannot
matter. The keeper still checks the exact path itself and 404s everything else.

**The away answer for an API caller.** `:8080` keeps serving the page, and now
answers `{"code":"workspace_sleeping"}` with a 503 when the caller asked for
JSON. The choice is keyed on `Accept` — and on `X-Format`, which is how
ingress-nginx passes the original `Accept` to an error backend — never on the
path: `/health` and `/api/keeper/wake` are both paths a browser lands on while
the workspace is away, and they must still get the page. That marker is what
`mobile/src/util/workspaceReach.ts` reads to tell "asleep" from "unreachable",
so `ingress-claude-api.yaml` also gains the keeper as its custom-error backend;
without it the app gets a raw nginx HTML 503 and can only say the vaguer thing.
This listener still never touches the Kubernetes API, so it reports no state —
"away" is all it can honestly say, and all the app needs before offering Wake.

**Not in step 4a:** the webhook and cron proxy, the queue and the wake limit
(step 4b, §4.5), and the idle loop (step 5).

### 4.5 The webhook and cron front door — implemented (step 4b)

A webhook or a schedule that fires while the workspace is asleep now runs its
task. Before this it did not: the request hit an ingress with no pod behind it
and got a raw nginx 503, and **GitHub does not retry a failed delivery**, so the
event was gone. A cron fire was worse — the CronJob's `curl -f` failed, the Job
retried twice and gave up, and the Triggers tab went on looking healthy.

The keeper now stands in front of the three receivers:

| Path | Caller | Reaches the keeper via |
|---|---|---|
| `POST /api/webhooks/<id>` | the internet | `ingress-webhooks.yaml`, repointed to `keeper-<user>-api` |
| `POST /api/triggers/cron-fire/<id>` | a CronJob pod | `WORKSPACE_INTERNAL_URL` |
| `POST /api/triggers/page-watch-check/<id>` | a CronJob pod | the same |

While the workspace is awake it forwards and mirrors the answer, so nothing
changes for a sender. While it is away it writes the request down, answers 202,
wakes the workspace and replays it unchanged. **The keeper never decides whether
a request is authentic**: it knows which ids exist, which is enough to answer
404 and wake nothing for a stranger walking URLs, and the signature is still
checked inside `server.py`, the only place that holds the secret. A request for
a *known* id with a bad signature therefore wakes the workspace and is then
rejected by it — the cost of a wake, not of a task.

**The decision table.** Decided from the outcome of the forward, never from
cached state alone; the state is a one-second-old guess that only picks the
first action.

| Cached state | Action | Outcome | Reply | Wakes? | Entry |
|---|---|---|---|---|---|
| unknown id | reject | — | 404, server.py's wording | **never** | none |
| `awake` | forward | a response | mirror status + body verbatim | no | deleted |
| `awake` | forward | **dial** failed | re-read state, re-decide | per new state | per new state |
| `awake` | forward | written, no answer | 202 | no | per kind (below) |
| `starting` | enqueue | — | 202 | **no** — it is already coming | kept |
| `asleep` | enqueue **then** wake | — | 202 | yes, if the budget allows | kept |
| `asleep`, budget spent | enqueue | — | 202 | **no** — drains at the next wake | kept |
| `stopped` | reject | — | 503 `workspace_stopped` | **never** | **none** |
| `Kube.Get` failed | enqueue | — | 202 | no | kept |

Four invariants make "loses no event" structural rather than argued:

1. **Durable before acknowledged.** The entry is written and `fsync`ed before
   the 202 and before the wake. Only that order closes the window the
   PersistentVolume exists for.
2. **One owner per entry.** A claim *is* a `rename` into `inflight/`, so of two
   goroutines racing for one entry exactly one gets it and a drain landing
   mid-forward cannot double-deliver. Measured: 8 goroutines over 60 entries
   gave 60 distinct claims, 0 duplicates and 366 cleanly lost races.
3. **Per-kind semantics, as a named constant.** Webhooks **at-least-once**: the
   sender's own delivery id and the replay cache absorb a duplicate, and a
   duplicated agent run is recoverable. Cron and page-watch **at-most-once**: an
   ambiguous entry is dropped with a log line, because `handle_cron_fire` has no
   dedup at all and two replays are two agent runs, while a missed tick simply
   fires again on schedule and `concurrencyPolicy: Forbid` makes that cheap.
4. **Per-provider TTL.** `slack` and `stripe` entries expire after 240 s with a
   line that says a signed timestamp went stale, rather than being replayed into
   a misleading `bad_signature` 404. `github`/`generic` keep for the configured
   hour.

**The one trap worth naming.** The forward buffers the body and re-sends it with
an explicit `Content-Length`, using a plain `http.Client` over a
`bytes.Reader` — **not** `httputil.ReverseProxy`. `handle_webhook_receive` reads
exactly `int(headers['Content-Length'])` (`handlers/triggers.py:156`) and
nothing in `server.py` de-chunks a request, so a chunked forward would run the
HMAC over an empty body and answer `404 bad_signature`: silent loss, and a
diagnosis that sends its owner hunting for a wrong secret. There is a test for
it, and the mutation that drops the explicit length turns that test red.

That is also why the cap is exactly 1 MiB, the same number as nginx's
`proxy-body-size: 1m` and `handle_webhook_receive`'s own check: a body of
exactly 1 MiB passes and one byte more is a 413 in server.py's own wording. If
the keeper's cap disagreed with either, one of them would be lying about what it
accepts.

**The wake budget** (`keeper.maxWakesPerHour`, default 10; `0` means
unlimited). Step 4b is the first time a request to a public URL from someone who
is not signed in can start a 4 GiB pod, so the cap arrives with that capability.
Exhausting it discards nothing — the request is queued and answered 202, and the
drain loop delivers it at the next wake from any cause, including the budget
freeing up. It does not apply to a person: a signed-in visit and the app's Wake
button are not what it protects against. The budget is persisted on the volume,
because a keeper that crashlooped would otherwise come back with a full budget
every few seconds, which turns a crash into an unlimited-wake primitive.

**The queue** lives on the volume 4.4 added, 0700 with 0600 files.
`keeper.queue.maxEntries` / `maxBytes` bound it (both, because 256 one-line
crons and 256 1 MiB webhooks are different shapes of the same problem); past
either the keeper refuses an away request with a visible 503 rather than
accepting bytes it has nowhere to keep, since a sender can retry a refusal and
cannot retry a silent loss. While the workspace is *awake* a full queue is
ignored instead: the entry is only crash insurance when the answer is being
mirrored back anyway.

**A queued cron entry holds a live `Authorization: Bearer <fire_token>` on
disk.** That is unavoidable if the replay is to be byte-identical — the token is
part of the request. Mode 0600 on a volume no other pod mounts, and nothing in
the queue ever logs a header value; there is a test that plants a fire token in
an entry, drives the paths that log about it, and fails if the token appears.

**`keeper.resources.limits.memory` rises from 32Mi to 128Mi**, because each
proxied body is buffered whole and `GOMEMLIMIT` is wired from that number. One
1 MiB body fits in 32Mi; several at once do not, and the old value would have
the keeper fighting the GC during exactly the burst it must not drop.
`keeper.queue.maxInFlight` (4) is the other half — the limit bounds the damage,
the semaphore bounds the demand — and it is acquired **after** the entry is
durable, because shedding load in front of the queue would lose the events the
queue exists to keep.

**Two chart rules without which every forward dies on a connect timeout.**
`networkpolicy.yaml` gains a third ingress rule admitting `app: keeper-<user>`
on 6080, and `keeper-networkpolicy.yaml` admits this workspace's CronJob pods on
8082 — matched on both `app` and `workspace-user`, so a co-tenant's cron pods
stay denied. Worse than the silent cron failure that rule's comment already
describes: here the keeper has already told the sender 202.

**Existing CronJobs self-heal.** Turning the keeper on changes where a fire has
to go, and the CronJobs already on the cluster have the old address baked into
their `curl`. Nothing would ever rewrite them — a CronJob is re-applied only
when someone edits its trigger — so a workspace whose crons all predate the
keeper would go on firing at a workspace that is asleep. Both `_apply_k8s`
methods now stamp `kube-coder.dev/internal-url`, and
`CronManager.reconcile_internal_url()` re-applies on a mismatch at boot, and on
nothing else. A missing annotation counts as a mismatch, so every pre-4b CronJob
repairs itself exactly once and then costs nothing. No new RBAC: the workspace
already holds get and patch on cronjobs (`serviceaccount.yaml:22`).

**One line of UX.** The status JSON gains `reason` (`visit` | `app` | `webhook`
| `cron`), set only while a wake the keeper started is still coming up, and the
holding page turns it into one sentence: *"A scheduled task is due, so it is
starting to run it."* A workspace that starts without anybody asking should say
which thing asked.

**What 4b does not change:** nothing on `:8080`, `:8081` or `:8083`; no mobile
code; and with `keeper.enabled=false` the chart renders identically to step 3 in
basic mode, and in oauth2 mode differs only by comment text in
`ingress-webhooks.yaml` — no rendered field.

#### What has been verified, and what has not

Stated plainly, so nobody reads a green suite as more than it is — the same rule
§6 sets out for step 3.

**Verified locally.** 122 Go tests, `gofmt`/`vet`/`-race` clean in WSL (Linux, Go
1.26.4 + gcc 15.2.0, 0 data races); 289 `helm unittest` cases across 30 suites;
`helm lint` clean with the keeper on and off; 14 new Python tests for the
annotation and the boot reconcile. And the whole chart rendered with
`keeper.enabled=false` was diffed against step 3's output: identical in basic
mode, and in oauth2 mode different only in comment text.

**Shown capable of failing.** Thirteen deliberate breakages were applied to a
copy of the source and each turned the suite red: skipping the trigger-id check,
forwarding without an explicit `Content-Length`, dropping the per-provider TTL,
claiming an entry without the rename, making every kind at-least-once, queueing
for a stopped workspace, ignoring the wake budget, forgetting it across a
restart, dropping the query string, forwarding only the first value of a
repeated header, and removing the page's wake reason. One of those found a real
defect in the suite rather than the code: the budget test passed *vacuously*,
because the one-second state cache answered "starting" to the second webhook and
the budget was never consulted. The test now disables that cache and the
mutation turns it red.

**Two things the in-process suite cannot catch, confirmed by running them
anyway.** Removing the `fsync` before the 202 leaves every test green — "durable"
versus "in the page cache" needs a real `kubectl delete pod`, not a reopened
file handle. Replacing `hmac.Equal` with `==` also leaves it green, which is
expected: the two agree on every input and differ only in timing. Both belong to
the cluster leg, and neither should be read as covered here.

**Not verified: everything that needs a cluster.** No live run has happened —
the queue surviving a killed keeper pod, the NetworkPolicy rules (meaningless
without a CNI that enforces them), a real GitHub delivery into a sleeping
workspace, the webhook ingress at all (it renders only under oauth2, which needs
a real HTTPS host), and the idle RSS with a buffered body in flight. The
cron and page-watch half *is* reachable on local minikube, since it goes through
an in-cluster Service rather than an ingress; the public-webhook half is not.

---

## 5. Staging plan

Ordered so each step is separately reviewable and separately revertible, and so
the two PRs that carry real risk come after the cheap ones.

| # | Scope | Depends on |
|---|---|---|
| 1 | **The idle contract** — `keeper_idle.py`, `GET /api/keeper/idle`, tests. No keeper, no behaviour change. | — (done) |
| 2 | **Decide §2.2 and §3.1.** Land the `replicas` ownership change and the chart's `keeper:` values block with everything defaulting off, so PR 3 has somewhere to plug in. `keeper.sleep.idleMinutes` → `KC_KEEPER_IDLE_MINUTES`. | — (done) |
| 3 | **The keeper itself** — replaces `holding-<user>`, a logged-in visit wakes a workspace it put to sleep, `keeper sleep`, RBAC, Deployment, image, CI job, release image (§4.3). No queue, no proxying, no idle loop. `keeper.enabled=false` renders byte-identical to before. | 2 (done) |
| 4a | **Trigger-ID and token-hash push** from `server.py`; the phone's `status`/`wake` on its own bearer ingress, and the `workspace_sleeping` JSON on the error path (§4.4). The app's Wake button starts working. No proxying, no queue. `keeper.enabled=false` still renders byte-identical. | 3 (done) |
| 4b | **The webhook and cron front door** — pass-through while awake; queue, wake and replay while asleep; the per-provider queue TTL; the wake limit; CronJob re-apply on URL change (§4.5). Wakes now work for webhooks and crons too. | 4a (done) |
| 5 | **Sleep** — the idle loop, `sleep.enabled=false` by default, controller sleeping-vs-stopped (which also closes §4.3's 0 -> 0 gap). The sleep record of §2.1 is the `managedFields` ownership step 3 already uses. The rate limiter moved to 4b, where the first unattended caller arrives. | 4b |
| 6 | **Mobile** — `workspaceState`, `WakeBanner`, the sleeping-503 JSON in `client.ts`, approval-queue flush, push-tap deferral, demo mode. Independent of 5's default. | done (#778); its keeper path went live with step 4a |

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
endpoint's auth gate and route wiring, `helm unittest` on the rendered chart
with the keeper on and off, and (step 3) the keeper's `go test -race` suite
against a fake API server that keeps `managedFields` the way the real one was
measured to. The cluster half of step 3 — RBAC with the real token, ingress
routing and auth, NetworkPolicy, idle memory — was run on minikube (§4.3).
