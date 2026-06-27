# How to deploy kube-coder on Kubernetes (multi-tenant, GitHub OAuth + TLS) — a step-by-step guide

**TL;DR:** This is the "real deployment" companion to the local minikube guide. It walks you from a Kubernetes cluster to a live, per-user kube-coder workspace at `https://<user>.dev.yourdomain.com/`, secured with **GitHub OAuth** and **automatic HTTPS** (cert-manager + Let's Encrypt). Each user gets an isolated pod, PVC, ingress, and assistant config from a single Helm chart.

If you just want to try kube-coder on your laptop first, do the [minikube guide](getting-started-minikube-macos.md) — no cloud, no DNS, no TLS. This guide is for putting it on a shared cluster for a team.

Repo: https://github.com/imran31415/kube-coder

---

## What you're building

A multi-tenant setup where each developer gets their own workspace:

```
   browser ──► oauth2-proxy ──► nginx-ingress ──► ws-<user> Service ──► ws-<user> Pod
                                                                         (server.py, code-server,
                                                                          ttyd, noVNC, tmux, AI)
```

- **One-time, cluster-wide:** an ingress controller, cert-manager, an image registry pull secret, and the kube-coder `base-infrastructure` Helm release.
- **Per user:** scaffold values → fill in DNS + GitHub OAuth → validate → `make deploy`.

Everything per-user is driven by `make <target> USER=<name>`, which auto-resolves that user's values + secrets files.

---

## Prerequisites

Cluster-side (you need these once):

| Requirement | Why |
|---|---|
| Kubernetes 1.19+ with `kubectl` configured | the cluster you'll deploy into |
| Helm 3.0+ | installs the charts |
| nginx-ingress controller | routing + per-host TLS + auth |
| cert-manager + a `ClusterIssuer` (e.g. `letsencrypt-production`) | automatic HTTPS certs |
| A container registry + image | the workspace image is ~2–3 GB; you pull it from a registry |
| A `regcred` image-pull Secret in the target namespace | lets the cluster pull that image |
| A wildcard or per-user DNS record pointing at the ingress IP | `*.dev.yourdomain.com` → ingress |

Workstation-side: `git`, `helm`, `kubectl`, and `openssl` (used to mint cookie secrets).

> **The image.** The reference deploy uses a DigitalOcean registry image, but any registry works (GHCR, ECR, GCR, Docker Hub, …). Build and push your own with `make push` (override `REGISTRY`/`IMAGE_NAME`), then reference that `repository:tag` in each user's values. The local minikube path builds inside the node and skips the registry entirely — the cloud path does not.

---

## Step 0 — Get the code

```bash
git clone https://github.com/imran31415/kube-coder.git
cd kube-coder
```

All commands below run from this directory.

---

## Step 1 — Make sure the cluster prereqs exist

Install these with their own official charts/manifests if you don't already have them:

```bash
# nginx ingress controller (example)
helm upgrade --install ingress-nginx ingress-nginx \
  --repo https://kubernetes.github.io/ingress-nginx \
  --namespace ingress-nginx --create-namespace

# cert-manager (example)
helm upgrade --install cert-manager cert-manager \
  --repo https://charts.jetstack.io \
  --namespace cert-manager --create-namespace \
  --set crds.enabled=true
```

Then create a `ClusterIssuer` (Let's Encrypt production) and confirm it's `Ready`:

```bash
kubectl get clusterissuer
```

Create the namespace and the registry pull secret kube-coder expects:

```bash
kubectl create namespace coder

kubectl create secret docker-registry regcred \
  --docker-server=<your-registry> \
  --docker-username=<user> \
  --docker-password=<token> \
  -n coder
```

> The default namespace is `coder` throughout the Makefile. Keep it unless you have a reason to change it.

---

## Step 2 — Deploy the base infrastructure (one time)

This installs the shared `base-infrastructure` Helm release (the `kaniko-wrapper` ConfigMap and shared bits the workspace pods mount):

```bash
make deploy-base
```

You only do this once per cluster, not per user.

---

## Step 3 — Create a GitHub OAuth App (per workspace)

OAuth is what gates access to a workspace. In GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App**:

- **Application name:** anything descriptive, e.g. `kube-coder — john`
- **Homepage URL:** `https://john.dev.yourdomain.com`
- **Authorization callback URL:** `https://john.dev.yourdomain.com/oauth2/callback`

Save it and note the **Client ID** and **Client Secret** — you'll paste them in a moment.

---

## Step 4 — Scaffold the user workspace

```bash
make new-user USER=john
```

This creates a private (gitignored) workspace skeleton under `users-private/john/`:

```
users-private/john/
├── values.yaml          # CHANGE-ME-laden, with a fresh cookieSecret already generated
└── secrets/
    └── oauth2.yaml      # holds the GitHub OAuth client id + secret
```

It also prints a checklist of exactly which fields to edit. The whole `users-private/` tree is gitignored by design, so your real secrets never get committed.

---

## Step 5 — Fill in the values

Open the scaffolded file and replace the `CHANGE ME` lines:

```bash
$EDITOR users-private/john/values.yaml
```

The fields that matter:

```yaml
user:
  name: john
  host: john.dev.yourdomain.com          # the DNS name for this workspace
  pvcSize: 50Gi
  env:
    - { name: GIT_USER_NAME,  value: "John Doe" }
    - { name: GIT_USER_EMAIL, value: "john@yourcompany.com" }

image:
  repository: <your-registry>/coder       # the image you pushed
  tag: <your-image-tag>
  pullPolicy: Always
  pullSecretName: regcred

ingress:
  className: nginx
  auth:
    type: oauth2                          # GitHub OAuth (recommended)
  tls:
    enabled: true
    secretName: john-dev-yourdomain-com-tls
    clusterIssuer: letsencrypt-production

oauth2:
  githubUsers: "john_doe"                 # comma-separated allowed GitHub usernames
  # or restrict by org/team instead:
  # githubOrg: "your-org"
  # githubTeam: "your-org:your-team"
```

Then drop the OAuth client credentials (from Step 3) into the gitignored secrets file:

```yaml
# users-private/john/secrets/oauth2.yaml
oauth2:
  clientId: "<github-oauth-client-id>"
  clientSecret: "<github-oauth-client-secret>"
  # cookieSecret was already generated by `make new-user`
```

You can also add optional secret files alongside `oauth2.yaml`, auto-included at deploy:

| File | Purpose |
|---|---|
| `claude.yaml` | sets `claude.apiKey` for pay-per-use Claude API access |
| `github-app.yaml` | sets `github.app.{appId,installationId,privateKey}` for private-repo access |

---

## Step 6 — Point DNS at the ingress

Find your ingress controller's external IP and create the A record:

```bash
kubectl -n ingress-nginx get svc ingress-nginx-controller
# john.dev.yourdomain.com  →  <EXTERNAL-IP>
```

A wildcard record (`*.dev.yourdomain.com → <EXTERNAL-IP>`) covers every future user in one shot.

---

## Step 7 — Validate, then deploy

The validator catches the common foot-guns *before* Helm runs — leftover `CHANGE ME` placeholders, a too-short cookie secret, DNS that doesn't resolve, a missing `regcred`, or a missing `base-infrastructure` release:

```bash
make validate-user USER=john
```

Fix anything it flags, then deploy (validation runs again automatically first):

```bash
make deploy USER=john
```

This does a `helm upgrade --install john-workspace ./charts/workspace` with the user's values + secrets, into the `coder` namespace, and waits for the rollout.

---

## Step 8 — Verify and log in

```bash
kubectl get pods,ingress,certificate -n coder | grep john
```

Wait for the pod to be `Running`, the ingress to have an address, and the certificate to be `Ready` (cert-manager issues it once DNS resolves — can take a minute or two on first deploy).

Then open:

```
https://john.dev.yourdomain.com/oauth/
```

You'll be bounced through GitHub OAuth; approve, and you land in the dashboard. The key routes:

| Surface | URL |
|---|---|
| Dashboard | `https://john.dev.yourdomain.com/oauth/` |
| VS Code | `https://john.dev.yourdomain.com/oauth/vscode/?folder=/home/dev` |
| Terminal | `https://john.dev.yourdomain.com/oauth/terminal/` |
| In-pod browser (noVNC) | `https://john.dev.yourdomain.com/oauth/vnc-direct/vnc.html` |

🎉 That's a fully isolated, OAuth-gated, TLS'd workspace running on your cluster.

---

## Adding more users

Repeat Steps 3–8 with a new `USER=`. Each user is fully independent — own pod, PVC, ingress, certificate, OAuth app:

```bash
make new-user USER=chase
$EDITOR users-private/chase/values.yaml          # + secrets/oauth2.yaml
make validate-user USER=chase
make deploy USER=chase
```

---

## Day-to-day operations

```bash
make deploy   USER=john     # helm upgrade --install (re-run after editing values)
make ship     USER=john     # build + push the user's image tag, deploy, force-roll the pod
make rollback USER=john     # helm rollback to the previous revision
make logs     USER=john     # tail the pod logs
make shell    USER=john     # exec into the IDE container
make test     USER=john     # node/yarn/gh/code-server sanity check
make status                 # helm + pod status across the namespace
```

`make help` lists everything.

### Updating the workspace image

When you bump the image (new CLI versions, dashboard changes), rebuild/push and roll the pod in one step:

```bash
make ship USER=john
```

`ship` reads the image `repository:tag` straight from that user's `values.yaml`, so the pushed image and the Helm deploy always stay in lockstep.

---

## Removing a user

```bash
helm uninstall john-workspace -n coder
kubectl delete pvc ws-john-home -n coder    # ⚠️ deletes the user's data — back up first
```

Then remove their DNS record and the GitHub OAuth app.

---

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Pod stuck `Pending` | `kubectl describe pod -n coder -l app=ws-john` — usually resources or PVC binding |
| `ImagePullBackOff` | `regcred` missing/wrong in `coder`, or `image.repository:tag` doesn't exist in the registry |
| Certificate not `Ready` | `kubectl describe certificate <name> -n coder` — check DNS resolves and cert-manager logs |
| OAuth login loops / 403 | callback URL must be exactly `https://<host>/oauth2/callback`; confirm your GitHub username is in `oauth2.githubUsers` |
| `validate-user` fails | it tells you which check failed (placeholders, cookie length, DNS, regcred, base release) — fix and re-run |
| Pod `ContainerCreating` forever | confirm `make deploy-base` ran and `base-infrastructure` is installed |

Full provisioning reference (resource sizing, org/team restrictions, lifecycle checklists): https://github.com/imran31415/kube-coder/blob/main/docs/NEW_USER_PROVISIONING.md

---

That's the whole multi-tenant path: base infra once, then a tight `new-user → edit → validate → deploy` loop per developer. Pair it with the [local minikube guide](getting-started-minikube-macos.md) for development. Questions/issues → open an issue on the repo.
</content>
