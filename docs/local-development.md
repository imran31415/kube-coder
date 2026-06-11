# Running kube-coder locally (minikube)

kube-coder normally runs on a managed Kubernetes cluster (the reference deploy
is DigitalOcean DOKS), but it runs just as well on a **local single-node
cluster** for development, evaluation, or offline use — no cloud account, no
container registry, no public DNS, and no TLS certificates required.

This guide uses **[minikube](https://minikube.sigs.k8s.io/)**, because its
built-in `ingress` addon ships the same `ingress-nginx` controller the chart
expects, and `minikube image load` lets you run a locally-built image without
pushing it anywhere. [kind](https://kind.sigs.k8s.io/) and
[k3d](https://k3d.io/) work too — see [Other local clusters](#other-local-clusters).

The whole flow is wrapped in `make local-*` targets that talk **only** to the
minikube context, so they can't touch a remote cluster.

---

## Prerequisites

| Tool | Why | Install (macOS) |
|------|-----|-----------------|
| Docker | minikube's driver + image build | Docker Desktop |
| minikube | the local cluster | `brew install minikube` |
| kubectl | talk to the cluster | `brew install kubectl` |
| helm | install the charts | `brew install helm` |

Give Docker Desktop enough headroom (Settings → Resources): the `make local`
target starts minikube with `--cpus=4 --memory=6g`, so allocate at least that
to Docker. The workspace image is large (~2–3 GB) because it bundles
code-server, Claude Code, OpenCode, Ante, LibreFang, ttyd, and a full toolchain.

> **Apple Silicon (M-series):** the build targets your host architecture
> automatically (`linux/arm64`), so it runs natively with no emulation. Every
> bundled binary (code-server, ttyd, LibreFang, …) has an arm64 build, so this
> is a first-class path, not a fallback.

---

## Quick start

```bash
# One command: start the cluster, build + load the image, deploy, print access info.
make local
```

That runs, in order: [`local-up`](#step-1-local-up) → [`local-build`](#step-2-local-build)
→ [`local-secret`](#step-3-local-secret) → [`local-deploy`](#step-4-local-deploy)
→ `local-info`. The first run takes a while (image build + load); later runs are cached.

When it finishes, `local-info` prints the three steps to reach the dashboard:

```bash
# 1. Map the hostname to localhost (one time, needs sudo):
echo '127.0.0.1  kube-coder.local' | sudo tee -a /etc/hosts

# 2. Forward the ingress controller (keep this running in its own terminal):
make local-forward

# 3. Open the dashboard and log in with basic auth admin / admin:
open http://kube-coder.local:8080/
```

That's it — you're in the kube-coder dashboard running entirely on your laptop.

> **Why port-forward instead of the minikube IP?** With the Docker driver on
> macOS and Windows, the minikube node IP isn't routable from the host. A
> `kubectl port-forward` to the ingress controller works identically on every
> OS, and routing still goes *through* the ingress (so host-based routing and
> basic auth behave exactly like production). On Linux you can instead point
> `/etc/hosts` at `$(minikube ip -p kube-coder)` and skip the port-forward.

---

## What each step does

You can run the steps individually (e.g. to rebuild after a code change).

### Step 1 — `local-up`
Starts the minikube profile `kube-coder` with the Docker driver and enables the
`ingress` addon (installs `ingress-nginx`), then waits for the controller pod.

### Step 2 — `local-build`
Builds the workspace image for your host arch (`docker buildx ... --load`) as
`kube-coder:local`, then `minikube image load`s it into the cluster. No registry
involved. Re-run this after editing the `Dockerfile` or anything baked into the
image (the dashboard SPA, installed CLIs).

### Step 3 — `local-secret`
Creates the `coder` namespace and a `kube-coder-basic-auth` Secret containing an
htpasswd entry for **admin / admin** (generated with `openssl passwd -apr1`).
The ingress references this Secret for HTTP basic auth. Change the credentials
by overriding `LOCAL_AUTH_USER` / `LOCAL_AUTH_PASS`:

```bash
make local-secret LOCAL_AUTH_USER=me LOCAL_AUTH_PASS=hunter2
```

### Step 4 — `local-deploy`
Installs the `base-infrastructure` chart (the `kaniko-wrapper` ConfigMap the
workspace pod mounts) and then the `workspace` chart using
[`deployments/local/values.yaml`](../deployments/local/values.yaml), and waits
for the `ws-local` rollout.

---

## Configuration

The local overlay lives at [`deployments/local/values.yaml`](../deployments/local/values.yaml)
and is safe to commit (no secrets). The local-specific choices:

| Setting | Local value | vs. cloud default |
|---------|-------------|-------------------|
| `image.repository` / `tag` | `kube-coder:local` | DO registry image |
| `image.pullPolicy` | `IfNotPresent` | `Always` |
| `image.pullSecretName` | _empty_ | `regcred` |
| `ingress.tls.enabled` | `false` | `true` (cert-manager + Let's Encrypt) |
| `ingress.auth.type` | `basic` | `oauth2` / `basic` |
| `user.host` | `kube-coder.local` | a real DNS name |
| `user.pvcSize` | `10Gi` | `50Gi` |
| `resources` | smaller | full |

### Adding your Claude / assistant keys

The workspace boots without any AI keys — you can log into Claude Code
interactively by running `claude` once in the pod terminal. To bake keys in,
drop a gitignored overlay at `secrets/local/keys.yaml`:

```yaml
claude:
  apiKey: "sk-ant-..."        # optional; or log in interactively instead
assistant:
  openrouter:
    apiKey: "sk-or-..."
```

…and add `-f secrets/local/keys.yaml` to the `local-deploy` helm command (or
re-run `helm upgrade` yourself). See [environment-variables.md](environment-variables.md)
and [llm-setup.md](llm-setup.md).

---

## Day-to-day

```bash
make local-info                     # reprint access details
make local-forward                  # (re)start the ingress port-forward
make local-build && make local-deploy   # rebuild image + redeploy after changes

# Direct cluster access (note: --context kube-coder, never your remote cluster)
kubectl --context kube-coder -n coder get pods
kubectl --context kube-coder -n coder logs -f deploy/ws-local -c ide
kubectl --context kube-coder -n coder exec -it deploy/ws-local -c ide -- bash
```

### Teardown

```bash
make local-down              # remove the kube-coder workspace, keep the cluster
make local-down DELETE=1     # also delete the minikube profile entirely
```

---

## Limitations of local mode

- **In-workspace image builds are disabled.** The `docker-build` helper uses
  Kaniko, which needs a registry to push to; the local overlay leaves the push
  target empty. kube-coder itself runs fine — you just can't build *and push*
  container images from inside a workspace. To enable it, run
  `minikube addons enable registry` and point `build.pushSecretName` /
  `build.defaultDestinationRepo` at it.
- **Single node, single workspace.** The local overlay provisions one workspace
  (`local`). The cloud per-user flow (`make new-user`, `make deploy USER=…`) is
  for multi-tenant clusters.
- **No TLS.** Local mode serves plain HTTP. That's fine on localhost; don't
  expose it.
- **Resources.** Defaults are laptop-sized; heavy builds/tests inside the
  workspace may want a bigger `minikube start --memory`.
- **Apple Silicon: the in-workspace browser/Desktop is degraded.** The image
  bundles Firefox via Mozilla's `linux64` (x86_64) build, which can't execute
  on an `arm64` node. The image still builds, and the **dashboard, terminal,
  tasks, Memory, Files, code-server, and Claude all work** — but the noVNC
  **Desktop** tab (which launches Firefox/X11) won't render. The dashboard
  opens on the Desktop tab by default, so just click **Build** (Tasks) or
  another tab. On `amd64` hosts the Desktop tab works normally.
- **Auth model.** Local uses http basic auth, where the ingress is the sole
  authenticator and server.py trusts requests that reach it (ingress-nginx
  strips the credential header and blocks re-forwarding it, so the backend
  can't re-verify). This is fine for a single-tenant local cluster; for
  multi-tenant clusters use `ingress.auth.type=oauth2`, where server.py
  enforces identity itself.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ErrImageNeverPull` / `ImagePullBackOff` on `ws-local` | The image wasn't loaded. Re-run `make local-build` (it both builds and `minikube image load`s). |
| `http://kube-coder.local:8080` won't resolve | Add the `/etc/hosts` line (`127.0.0.1 kube-coder.local`) and make sure `make local-forward` is running. |
| 401 / auth prompt loops | Credentials are `admin` / `admin` (or whatever you set via `LOCAL_AUTH_*`). Re-run `make local-secret` if you changed them. |
| Pod stuck `ContainerCreating` | Usually the PVC or the `kaniko-wrapper` ConfigMap — confirm `make local-deploy` installed `base-infrastructure`. |
| minikube won't start / OOMs | Raise Docker Desktop's memory, or lower it: `make local-up` then `minikube start -p kube-coder --memory=4g`. |

---

## Other local clusters

The chart is cluster-agnostic; only the `make local-*` wrappers assume minikube.
To use a different tool, replicate the four steps against it:

- **kind:** create the cluster, install `ingress-nginx`
  (`kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml`),
  load the image with `kind load docker-image kube-coder:local`, then
  `helm upgrade --install` with `deployments/local/values.yaml`.
- **k3d:** `k3d cluster create --k3s-arg "--disable=traefik@server:0"`, install
  `ingress-nginx`, push to the built-in `k3d` registry (or import the image),
  then deploy the same overlay.

The `deployments/local/values.yaml` overlay works unchanged on any of them.
