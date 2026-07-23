# workspace-controller

A small admin console for a kube-coder namespace: list every workspace, start/stop
any of them, and view per-workspace usage metrics. Deployed **once per namespace**
(like `base-infrastructure`), not per user.

## What it does

- **List + status** — shows every `ws-*` Deployment with live state
  (running / stopped / transitioning / degraded) and pod health.
- **Start / stop** — scales the workspace Deployment to `1` / `0`. This is a pure
  Kubernetes API operation (`kubectl scale`) — there is no Helm at runtime — so it
  preserves the workspace's PVC and is fully reversible.
- **Usage metrics** — an expandable mini dashboard per row plus a detail page
  (`#/w/<user>`) with a 1h/6h/24h/7d selector showing CPU, memory, disk, network,
  uptime, and a rough cost estimate, sourced from the in-cluster Prometheus.
- **Cluster capacity** — a top-level rollup (`GET /api/capacity`) showing how
  workspace usage stacks against node allocatable capacity, cluster-wide and
  per-node, with a history chart so you can spot a previous spike or see how
  close you are to running out of headroom. Capacity (`kube_node_status_allocatable`)
  and usage both come from Prometheus, so it needs **no extra RBAC** — the
  controller's Role still can't read `nodes` (see below). On a shared cluster the
  bar separates *workspace* usage from *other tenants* sharing the same nodes, so
  "total usage vs allocatable" is honest headroom rather than workspaces alone.
- **Edit CPU/memory limits** — the detail page has an *Edit limits* control that
  patches the `ide` container's CPU + memory limits in place
  (`POST /api/workspaces/<user>/resources`). Like start/stop it's a live
  `kubectl patch` (a strategic merge that touches only that container's `limits`,
  leaving `requests` and other containers alone) — so it takes effect immediately
  by rolling the pod, and like start/stop a later `helm upgrade` resets it;
  durable changes still belong in the workspace's `values.yaml`. Bounded by
  `MAX_CPU_LIMIT_CORES` / `MAX_MEM_LIMIT` so a typo can't request an
  unschedulable pod.
- **Restart & pull latest** — when a newer release exists, the row shows an
  *update* badge and the detail page an **Updates** card that repoints the
  workspace at the latest image (`POST /api/workspaces/<user>/update`). "Latest"
  comes from the GitHub Releases of `RELEASE_REPO`; the live Deployment is patched
  and, when GitOps is configured, the new tag is also committed to the user's
  `values.yaml` so it survives the next reconcile. End users can self-update their
  own workspace from the workspace dashboard when self-serve is enabled — that path
  runs on a **separate, token-gated listener** so it never touches the admin API.
  See [docs/workspace-updates.md](../../docs/workspace-updates.md) for enablement
  and an end-to-end validation walkthrough.
- **Provision a workspace** *(optional, off by default)* — type a GitHub
  username and the controller registers a **GitHub App** for them via the
  manifest flow (one in-browser confirmation click), pushes rendered
  `values.yaml` + `secrets/oauth2.yaml` to a private GitOps repo, and launches a
  short-lived **privileged Job** that runs `helm upgrade`. The always-on
  controller never holds workspace write power itself — it only validates input,
  does the manifest exchange, pushes to git, and `create`s the Job (which assumes
  a separate `workspace-provisioner` ServiceAccount). See **Provisioning** below.

## Architecture

```
ingress ──▶ oauth2-proxy (reverse-proxy, --github-user gate) ──▶ controller (:8080)
                                                                    │
                                          kubectl (in-cluster SA) ──┤── k8s API: list / scale
                                                  HTTP query  ──────┴── Prometheus: usage metrics
```

- **Backend** `controller.py` — stdlib `http.server`, runs on the existing coder
  image (already has python3 + kubectl). Ships via ConfigMap.
- **Frontend** `web/` — Preact + Vite, built to a single inlined `index.html`
  (`vite-plugin-singlefile`) and shipped as one ConfigMap key.
- **Auth** — oauth2-proxy in reverse-proxy mode gates access to a GitHub-user
  allowlist; the controller trusts the `X-Forwarded-User` it injects
  (`TRUSTED_PROXY`). A NetworkPolicy ensures only that oauth2-proxy can reach the
  backend, so the trusted header can't be forged by other pods in the namespace.
- **RBAC** — namespace-scoped Role (never ClusterRole): `deployments`
  (get/list/watch) + the separate `deployments/scale` subresource
  (get/update/patch) + `deployments` `patch` (for in-place limit edits), plus
  `pods`/`ingresses`/`persistentvolumeclaims` reads for status, links, and disk
  size. No `metrics.k8s.io` (this cluster has no metrics-server — metrics come
  from Prometheus over HTTP, needing no k8s RBAC). When `provision.enabled`, the
  controller additionally gets `batch/jobs` (get/list/watch/**create**) — and a
  *separate* `workspace-provisioner` SA gets the broad create/update the Job
  needs, so that power never sits directly on the internet-facing controller pod.
  See **[Provisioning privilege model](#provisioning-privilege-model)** for the
  honest blast radius and the guardrails that constrain it.

## Prerequisites (one-time)

- A DNS host for the console (`controller.host`).
- A **dedicated** GitHub OAuth App; callback `https://<host>/oauth2/callback`.
- A 32-char cookie secret: `openssl rand -base64 64 | tr -d '\n=+/' | head -c 32`.
- `oauth2.githubUsers` — the allowlist; this is the access gate.

Put non-secret values in `users-private/_controller/values.yaml` and the oauth2
credentials in `users-private/_controller/secrets/oauth2.yaml` (both gitignored).

## Deploy

```sh
make ship-controller-config   # build SPA → helm upgrade → roll the pod
```

Other targets: `make controller-web` (build SPA only), `make deploy-controller`
(helm upgrade only), `make controller-dev` (run the backend locally against your
kubeconfig; listing is read-only and safe).

## Provisioning (optional)

Off unless `provision.enabled=true` and every field below is supplied; otherwise
the *New workspace* button is hidden and the endpoints return `501`.

**Flow.** Admin enters a username → controller validates it against the GitHub
API → the SPA POSTs a GitHub App *manifest* to `github.com` → admin clicks
*Create GitHub App* → GitHub redirects to `/api/provision/github/callback` with a
one-time code → the controller exchanges it for the app's `client_id` /
`client_secret`, generates a cookie secret, renders + pushes
`users-private/<slug>/{values.yaml,secrets/oauth2.yaml}` to the GitOps repo, and
launches the provisioner Job, which `make deploy`s the workspace. The SPA polls
`/api/provision/<slug>/status` until the pod is running.

GitHub has **no API to create classic OAuth Apps**, which is why this uses
**GitHub Apps** — their `client_id`/`client_secret` drive the exact same
`oauth2-proxy --provider=github` login, so nothing downstream changes.

**One-time setup** — run the scaffold, then follow its printed steps. Full
walkthrough in [`docs/PROVISIONING.md`](../../docs/PROVISIONING.md):

```sh
scripts/setup-controller-provisioning.sh \
  --domain dev.example.io \
  --gitops-repo github.com/<you>/kube-coder-users.git
```

It generates the `state-secret`, writes the gitignored
`users-private/_controller/secrets/provision.yaml`, and prints the `provision:`
block to add to your controller values plus the remaining manual steps:

- Wildcard DNS `*.<provision.workspaceDomain>` pointing at the ingress.
- A **private** GitOps repo (`provision.gitops.repo`) with an initial commit.
- A push token for that repo (Contents: RW) + GitHub API read, in `provision.gitToken`.
- The signed-in admin must be able to create GitHub Apps under their account (or
  `provision.githubOrg`).

> ⚠️ Generated config (including the client secret + cookie secret) is committed
> to the GitOps repo. Keep it private; layering sealed-secrets/SOPS is recommended
> (point `provision.existingSecretName` at a Secret you manage out-of-band).

| Provisioning key | Purpose |
|---|---|
| `provision.enabled` | Master switch (default `false`) |
| `provision.workspaceDomain` | `<login>.<domain>` host for new workspaces |
| `provision.githubOrg` | Org to create the GitHub Apps under (empty = admin's account) |
| `provision.gitops.repo` / `.branch` | Private repo (host/path, no scheme) the config is pushed to |
| `provision.chart.repo` / `.ref` | Where the Job pulls the `workspace` chart from (your fork if customised) |
| `provision.gitToken` / `.stateSecret` | Runtime creds — set via the gitignored secrets overlay |
| `provision.existingSecretName` | Use a Secret you manage instead of chart-rendering one |
| `provision.serviceAccount` | SA the privileged Job runs as |
| `provision.image` | Job image (empty = controller image; installs helm on the fly) |
| `provision.admissionPolicy.enabled` | Ship the provisioner-Job ValidatingAdmissionPolicy (default on; needs k8s ≥ 1.30) |

### Provisioning privilege model

Stated honestly (security review July 2026, finding 4). Provisioning splits
privilege deliberately: the always-on, internet-facing **controller** holds no
cluster-wide write verbs — only namespaced `create jobs` in its own namespace.
The broad cluster-wide ClusterRole (create/update on namespaces, deployments,
services, PVCs, configmaps, secrets, serviceaccounts, roles, rolebindings,
ingresses, networkpolicies, jobs) lives on a *separate* short-lived
`workspace-provisioner` ServiceAccount that only the provisioner Job runs as.

**The catch:** in Kubernetes, a principal that can create a workload *and* select
another ServiceAccount in the same namespace effectively inherits that SA's
identity — the kubelet mounts a token for whatever SA the Job names. So a
controller compromise (RCE, k8s-token theft, or any flaw giving arbitrary
Job-manifest control) does **not** stay "can start Jobs"; it bridges to the full
provisioner ClusterRole. This is a conditional privilege-escalation path, not a
standalone RCE, and it only exists when `provision.enabled=true` (off by default).

Guardrails shipped here (defense-in-depth):

- **`ValidatingAdmissionPolicy`** (`templates/provisioner-vap.yaml`, on by default
  when provisioning is enabled) pins the shape of any Job that runs as the
  provisioner SA: exact SA name, approved image repository, exactly one expected
  container (no extra/init/ephemeral containers), and no privileged
  securityContext / added capabilities / hostPath / hostNetwork|PID|IPC. A
  tampered manifest therefore cannot run an attacker-controlled workload under the
  provisioner identity. Requires k8s ≥ 1.30; disable via
  `provision.admissionPolicy.enabled=false` on older clusters.
- **No cluster-wide namespace `delete`** on the provisioner ClusterRole — normal
  provisioning only creates+labels namespaces; teardown is a manual admin runbook.
- **`automountServiceAccountToken: false`** on oauth2-proxy (it never calls the
  k8s API), removing a standing token an attacker could otherwise lift.

**Recommended endgame (not yet implemented):** move the privileged provisioner
into a *separate* namespace fronted by a constrained broker service that builds
Jobs from an immutable, in-cluster template — so the internet-facing controller
never shares a namespace with the provisioner SA and cannot express a Job
manifest at all. That is a multi-service refactor tracked as follow-up; the VAP
above is the mergeable interim mitigation.

## Key values

| Key | Purpose |
|---|---|
| `controller.host` | DNS host the console is served at (required) |
| `oauth2.githubUsers` | Comma-separated GitHub logins allowed to sign in (the gate) |
| `controller.adminUsers` | In-app allowlist double-check (defense-in-depth) |
| `controller.metrics.prometheusUrl` | In-cluster Prometheus; empty disables metrics |
| `controller.metrics.cost.*` | Rough cost-estimate rates (core-hour / GB-hour / GB-month) |

Metrics note: live CPU/memory/disk/network exist only for **running** workspaces
(Prometheus reports mounted volumes / active pods); stopped workspaces show disk
capacity and storage cost, the rest as “—”.
