# Evaluating `kubernetes-sigs/agent-sandbox` as the workspace runtime

**Status:** decided · **Date:** 2026-08-17 · **Issue:** #611
**Verdict: BORROW** — implement pause/resume ourselves against our own Deployment (#612). Do not adopt the `Sandbox` CRD yet. Revisit at v1, or earlier if the triggers in [What would change the answer](#what-would-change-the-answer) fire.

---

## TL;DR

`agent-sandbox` is aimed squarely at our workload shape and is worth watching. But the one capability that would justify taking the dependency — **automatic resume on network activity — is not implemented upstream.** It sits in the README's "Desired Sandbox Characteristics" list, alongside "memory sharing across sandboxes." What ships today is a manual `spec.operatingMode: Running|Suspended` flag.

Against our own Deployment, that flag is `kubectl scale --replicas=0`. We would be taking a cluster-scoped CRD + controller prerequisite, and a v1beta1 API that has already renamed itself once, in exchange for something we can already do — while still having to write our own activator for the part we actually want.

Separately, the pod was never the hard part. `Sandbox` models the pod extremely well and models **none** of the other 80% of a workspace.

---

## What it actually is — verified from source

Read from `api/v1beta1/sandbox_types.go` at `main`, not from docs or talks.

```go
type SandboxSpec struct {
    SandboxBlueprint `json:",inline"`   // PodTemplate, VolumeClaimTemplates, Service *bool
    Lifecycle        `json:",inline"`   // ShutdownTime *metav1.Time, ShutdownPolicy *ShutdownPolicy
    OperatingMode    SandboxOperatingMode `json:"operatingMode,omitempty"`
}

const (
    SandboxOperatingModeRunning   SandboxOperatingMode = "Running"
    SandboxOperatingModeSuspended SandboxOperatingMode = "Suspended"
)
```

Three corrections to assumptions that were circulating when this spike was opened:

| Assumption | Reality |
|---|---|
| "Controller handles pause **and automatic resume on network activity**" | **Not shipped.** `operatingMode` is a manual flag. Auto-resume and deep hibernation are in the README's *aspirational* "Desired Sandbox Characteristics" section |
| "Scheduled deletion doesn't fit us" | **Not a problem.** `ShutdownTime` is a nil-able pointer; omit it and nothing expires. `ShutdownPolicy: Retain` also exists |
| "How much survives inside `spec.podTemplate`?" | **All of it.** `PodTemplate.Spec` is `corev1.PodSpec` verbatim. Only *metadata* is narrowed (labels + annotations only) |

There is also no `replicas` field on `SandboxSpec` — it is a true singleton, matching our `replicas: 1`. (`replicas` exists on `SandboxWarmPool`, a different CRD.) The presence of `sandbox_conversion.go` confirms the v1alpha1 → v1beta1 rename is real and carries conversion machinery.

---

## The five questions

### 1. Does the `Sandbox` lifecycle fit a long-lived dev workspace?

**Partly — and the half that fits is the half we don't need help with.**

- **Suspend/resume:** fits. A workspace is a singleton stateful pod with a PVC; `operatingMode` is exactly the right verb.
- **Scheduled deletion:** a non-issue, not a misfit. We simply never set `shutdownTime`. It is opt-in by nil.
- **Auto-resume:** the actual gap. Ephemeral agent runtimes are woken by their orchestrator. A developer workspace is woken by *a human opening a browser tab*, which is what wake-on-request means for us — and upstream does not implement it.

So the lifecycle model fits, but adopting it does not deliver the lifecycle *feature* we want.

### 2. What do we lose? How much survives inside `spec.podTemplate`?

**The whole pod survives. That is the problem — the pod is ~19% of a workspace.**

Because `PodTemplate.Spec` is a verbatim `corev1.PodSpec`, everything in [`deployment.yaml`](../charts/workspace/templates/deployment.yaml) transfers unchanged: all three containers (`ide`, `dind`, the `github-app-token` sidecar), every volume, `imagePullSecrets`, `serviceAccountName`, `securityContext` — and `runtimeClassName`, which is why #613 stands on its own regardless of this decision.

What `Sandbox` does **not** model, from `charts/workspace/templates/` (~3,390 lines of templates total):

| Stays ours either way | Lines |
|---|---|
| 6 × Ingress (`ingress`, `-oauth2`, `-public`, `-claude-api`, `-gateway`, `-webhooks`) | 884 |
| entrypoint / ssh-server / terminal-entry / holding / claude / browser ConfigMaps | 566 |
| GitHub App token refresh | 494 |
| NetworkPolicy ingress + egress | 357 |
| `oauth2-proxy` Deployment | 205 |
| ResourceQuota, Service, ServiceAccount, PVC, Secrets, RBAC | 253 |
| **Total unmodelled** | **2,759** |
| *Modelled by `spec.podTemplate`* | *632* |

Adoption replaces roughly a 10-line Deployment wrapper — `replicas: 1`, `strategy: Recreate`, selector — and leaves everything else exactly as it is. The auth edge, the network posture, and the boot sequence are what make a kube-coder workspace a workspace, and `Sandbox` is silent on all of them, correctly so; they are outside its scope.

We would also inherit a coupling: `spec.service` creates a headless Service, and our Service/Ingress/NetworkPolicy all select on `app: ws-<user>` ([`service.yaml`](../charts/workspace/templates/service.yaml)). We would set `service: false` and pin the labels through `podTemplate.metadata.labels`. Workable, but it is new surface to keep correct on every upstream release.

### 3. Does it conflict with #421?

**No. They do not overlap at all.**

#421's `ProvisionRequest` is a **single-field spec (`slug`)** whose entire purpose is to deny the controller the ability to inject image/command/env/volume/SA overrides when creating a *privileged provisioning Job*. It is a constrained RPC to a privileged broker in another namespace.

`Sandbox` is the *workspace object*. Different noun, different lifecycle, different threat model — `ProvisionRequest` derives its security value precisely from **not** being a general pod-shaped API, which is exactly what `Sandbox` is.

The premise in #611 that "#421 may shrink to just the privileged provisioning broker" does not apply: #421 *already is* only that. Adopting `Sandbox` would not remove a single line from it.

### 4. What is the real maturity?

v0.1.0-era, `v1beta1`, with one completed API-group migration already behind it and a conversion file in-tree to prove it. The project is credible — SIG Apps, ~3.6k stars, active commit history — and this is a normal place for a young CRD to be.

But the asymmetry matters: **we would own the migration for every existing workspace, every time upstream renames a field.** Each rename is a coordinated change across the chart, the controller, and every live PVC-backed workspace in the fleet. That is an unbounded liability accepted in exchange for, today, a flag we can already set ourselves.

### 5. Operator burden

Installing `agent-sandbox` is **cluster-scoped**: CRDs plus a controller, before the first workspace can start. kube-coder's selling point is that it installs on a stock cluster.

This lands directly against #580, which shipped `make doctor` (PR #590) precisely to shrink the prerequisite list — nginx-ingress, cert-manager, wildcard DNS, `regcred`. Adopting `Sandbox` adds a seventh prerequisite and a new `make doctor` check whose remediation is "go install a second operator." That is a real regression in the first-run experience, paid by every operator, to benefit a feature that is opt-in per workspace.

---

## Decision

**Borrow.** Build pause/resume ourselves under #612, against our own Deployment.

The supporting fact is that we are already most of the way there. [`controller.py`](../charts/workspace-controller/controller.py) already detects idle workspaces — `cpu_max < INSIGHTS_IDLE_CPU_CORES` (0.05 cores) over a 6-hour window — and already computes what stopping one would save:

```python
add('info', 'idle',
    f"{user}'s workspace has been idle (CPU under {int(INSIGHTS_IDLE_CPU_CORES * 1000)}m) "
    f"for the last {hstr}.{tail}")
# ...where `tail` is " Stopping it would free compute (~$N/mo)."
```

We compute the saving, tell the operator the dollar figure, and then offer no way to take it. #612 is largely wiring that existing signal to a `replicas: 0` action and a wake path — not new infrastructure.

**What to borrow from their design, concretely:**

1. **`operatingMode` as an explicit field, not inferred state.** Desired state is declared and observable; the controller reconciles toward it. Do not encode "paused" as an incidental `replicas: 0`.
2. **Retain-by-default semantics.** Their `ShutdownPolicy: Retain` is the right default for anything touching a home directory. Pausing must never be one bug away from deleting.
3. **Status surface.** `Conditions`, `PodIPs`, `NodeName`, `ServiceFQDN` — a good shape for what the console should show about a paused workspace.
4. **Their vocabulary.** If we later adopt, having used `Running`/`Suspended` makes the migration mechanical.

**On the #612 wake path:** its option 3 was "adopt Agent Sandbox's controller." Given auto-resume is unimplemented upstream, **option 3 buys nothing today** and should be closed. The choice is between an activator in the ingress path and console-only manual wake.

---

## What would change the answer

Revisit — and re-run this evaluation — if any of these become true:

1. **Auto-resume on network connection ships and is stable.** This is the big one. It is the only capability here we cannot cheaply build ourselves, and it is the reason to accept the dependency.
2. **Pod snapshots ship with restore across nodes.** Snapshot/restore is genuinely hard; a working implementation would be worth real integration cost.
3. **The API reaches v1** with a compatibility guarantee, removing the open-ended migration liability from question 4.
4. **A managed control plane ships it by default** (e.g. DOKS/GKE installing the controller), which erases the question-5 operator burden entirely.
5. **We find ourselves building warm pools.** `SandboxWarmPool` + `SandboxClaim` solve pre-warmed provisioning, and rebuilding that is a much larger lift than rebuilding a pause flag.

Triggers 1 and 3 together would likely flip this to **adopt**.

---

## Verification appendix

**Verified from upstream source** (`api/v1beta1/sandbox_types.go`, README, `main` branch):
`SandboxSpec` / `SandboxStatus` field lists · `SandboxOperatingMode` constants · `ShutdownPolicy` constants · `PodTemplate.Spec` being `corev1.PodSpec` · absence of `replicas` on `SandboxSpec` · auto-resume and deep hibernation being aspirational · existence of `sandbox_conversion.go`.

**Verified against this repo:** chart composition and line counts · pod container set · `app: ws-<user>` selector coupling · existing idle detection and its cost estimate.

**Not verified at runtime.** No PoC was run: question 2 was answerable directly from the type definitions, which is the condition #611 set for needing one. The following would sharpen a future *adopt* decision but do not change this one, since each is a cost that argues further against adoption rather than for it:

| # | Open question | Why it matters |
|---|---|---|
| 1 | Does deleting a `Sandbox` garbage-collect the PVC from `volumeClaimTemplates`? | If yes, a `kubectl delete` destroys a home directory. Must be answered **before** any migration |
| 2 | Would the controller adopt an existing PVC, or create a new one? | Determines whether migrating live workspaces is even possible without data movement |
| 3 | `dind-storage` is an `emptyDir` — every cached Docker layer is lost on suspend | Applies to #612 **regardless of this verdict**; measure first-build-after-resume cost |
| 4 | RWO PVC rescheduling to another node on resume | Stuck-attach risk on multi-node clusters; DOKS is multi-node |
| 5 | Does the install need cert-manager or a conversion webhook? | Sizes the question-5 operator burden precisely |
| 6 | Cold vs warm resume latency | The number #612 needs to justify auto-pause at all |

Rows 3, 4 and 6 should be picked up by #612 on our own implementation, where they apply just as much.
