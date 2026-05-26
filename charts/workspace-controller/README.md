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
  (get/update/patch), plus `pods`/`ingresses`/`persistentvolumeclaims` reads for
  status, links, and disk size. No `metrics.k8s.io` (this cluster has no
  metrics-server — metrics come from Prometheus over HTTP, needing no k8s RBAC).

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
