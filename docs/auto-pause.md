# Auto-pause idle workspaces

A per-user dev environment is idle most of the day and all night, but its pod
keeps its CPU and memory reserved the whole time. Auto-pause scales an idle
workspace's pod to **0 replicas** and leaves everything else alone — the PVC,
the ingress, the secrets, the TLS certificate.

**It is off by default and opt-in per workspace.** Issue #612.

## Read this first: waking is manual

> **A request to a paused workspace fails.** It does not queue, it does not
> wake the workspace, and it does not retry. The workspace stays down until
> someone presses **Start** in the console (or runs `make start USER=<name>`).

There is no wake-on-request. Real wake-on-request needs a component that sits
in the request path while the pod is down, holds the connection open, scales the
Deployment back to 1, waits for readiness and then proxies — a
Knative-activator-shaped thing, always on, in front of every workspace. That is
a much larger decision than the pause itself, and the saving does not depend on
it: the money is in the pod being down, not in how it comes back up.

Turn auto-pause on for workspaces where "it's off until I turn it on" is an
acceptable trade for the compute bill. Leave it off for anything that has to
answer an unattended request — a webhook target, a cron trigger, a demo URL
someone might click.

## What is preserved

Everything except the running process:

| | |
|---|---|
| **Home volume** (`ws-<user>-home`) | Preserved. It is a separate PVC carrying `helm.sh/resource-policy: keep`, so neither a pause nor a `helm uninstall` deletes it. |
| Ingress, TLS certificate, DNS | Untouched — the URL keeps resolving, it just has nothing behind it. |
| Secrets, OAuth cookie secret | Untouched. |
| Running processes, tmux sessions, `/tmp` | **Lost.** The pod is gone; only what is on the PVC survives. |

Pausing never deletes anything. Backup and restore of the home volume is a
separate concern (#579).

## When it will not pause

A pause is only allowed when the workspace itself reports that it is not busy.
The workspace publishes its own activity onto its pod's annotations every 30s,
and the controller reads that back. **It will not pause while:**

- a Build is `running` **or** `waiting-for-input` — an agent parked at a
  permission prompt burns no CPU and looks idle from outside, but its run is
  mid-flight
- a Hypervisor chat turn is in flight
- a terminal is attached (any tmux client — the web terminal, the mobile app, or SSH)
- measured CPU is above the idle threshold — this catches a dev server or a
  compile left running, which none of the signals above know about
- the activity signal is **missing or stale** — a workspace on an older image,
  or one whose beacon has stopped, is never paused
- Prometheus cannot be reached to confirm idleness

Every one of those fails in the same direction: **when in doubt, keep running.**
Leaving a pod up costs a few cents; scaling one down mid-run costs the user
their work.

## Turning it on

**Console** — open the workspace, tick *Auto-pause when idle*, set the
threshold, Save. This patches the live Deployment and, when GitOps is
configured, also commits the setting to the user's `values.yaml` so the next
reconcile does not undo it.

**Chart** — in the user's `values.yaml`:

```yaml
autoPause:
  enabled: true
  idleMinutes: 120
```

The annotations live on the Deployment's own metadata, not the pod template, so
changing the policy **does not restart the workspace**.

## Waking a paused workspace

- Console: press **Start** on the row (an auto-paused workspace is badged
  `auto-paused`, which is how you tell it from one somebody stopped by hand)
- CLI: `make start USER=<name>`

Starting clears the auto-paused marker. The workspace comes back with its home
directory exactly as it was.

## Tuning

| Setting | Where | Default | What it does |
|---|---|---|---|
| `autoPause.enabled` | workspace values | `false` | Opt this workspace in. |
| `autoPause.idleMinutes` | workspace values | `120` | Idle minutes before pausing. Separate from the console's 6h *advisory* window, which only decides when to **tell** an operator. |
| `AUTOPAUSE_ENABLED` | controller env | `true` | Fleet kill switch. |
| `AUTOPAUSE_INTERVAL_SECONDS` | controller env | `300` | How often the sweep runs. |
| `AUTOPAUSE_BEACON_MAX_AGE_SECONDS` | controller env | `300` | How old an activity signal may be and still be believed. Must comfortably exceed `KC_BEACON_INTERVAL`. |
| `KC_BEACON_INTERVAL` | workspace env | `30` | How often the workspace publishes its activity. |

## How it works

```
workspace pod                          workspace-controller
─────────────                          ────────────────────
ActivityBeacon (every 30s)             AutoPauser (every 5m)
  live Builds? chat turn?                for each opted-in workspace:
  tmux client attached?                    read the pod's annotations
        │                                  fresh? not busy? idle > threshold?
        ▼                                  CPU confirms idle?
  kubectl patch pod                              │
    kube-coder.io/busy          ───────────►     ▼
    kube-coder.io/last-activity          kubectl scale --replicas=0
    kube-coder.io/beacon-at              + mark kube-coder.io/auto-paused-at
```

The workspace annotates **its own pod** — it has `patch` on pods in its own
namespace and nothing more. The controller never talks to a workspace; it reads
Kubernetes objects it already lists and Prometheus, which is why the workspace
has to publish the answer rather than be asked for it.

`beacon-at` is the freshness proof, and it is what makes the whole thing safe to
run: a beacon that has stopped updating reads as busy, so the failure mode of
every bug in the publishing path is "this workspace never pauses".
