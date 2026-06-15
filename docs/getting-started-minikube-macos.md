# How to get started with kube-coder on a MacBook (with minikube) — a step-by-step guide

**TL;DR:** kube-coder gives you a full browser-based dev workspace (VS Code in the browser, persistent terminals, an in-pod browser, AI assistants like Claude Code/OpenCode, a dashboard) running on Kubernetes. You can run the *whole thing locally* on your Mac with minikube — no cloud account, no registry, no DNS, no TLS. This walks you from a fresh laptop to the dashboard in your browser.

Repo: https://github.com/imran31415/kube-coder

---

## What you're building

A single-node Kubernetes cluster on your laptop running one kube-coder workspace pod, reachable at `http://kube-coder.local:8080/` with basic auth (`admin` / `admin`). Everything is wrapped in `make local-*` targets that talk **only** to the minikube context, so they can't accidentally touch a real cluster.

Heads up before you start:
- The workspace image is **big (~2–3 GB)** — it bundles code-server, Claude Code, OpenCode, Ante, LibreFang, ttyd and a full toolchain. The first build takes a while; later runs are cached.
- minikube boots with `--cpus=4 --memory=6g`, so give Docker Desktop at least that.
- **Apple Silicon (M1/M2/M3/M4):** fully supported and runs **natively on arm64** (no emulation). One caveat: the in-pod *Desktop/browser (noVNC)* tab won't render because the bundled Firefox is an x86_64 build — but the dashboard, terminal, tasks, Memory, Files, code-server, and Claude all work fine. On Intel Macs the Desktop tab works too.

---

## Step 0 — Install the prerequisites

You need four tools: Docker, minikube, kubectl, helm.

**Docker Desktop** — download from https://www.docker.com/products/docker-desktop/ , install, launch it, and make sure the whale icon in the menu bar says it's running. Then give it headroom:
> Docker Desktop → Settings → Resources → set CPUs ≥ 4 and Memory ≥ 6 GB → Apply & Restart.

**The CLI tools** — easiest via [Homebrew](https://brew.sh). If you don't have brew:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Then:

```bash
brew install minikube kubectl helm
```

Verify everything is on your PATH:

```bash
docker version        # should talk to the daemon (Docker Desktop running)
minikube version
kubectl version --client
helm version
```

If any command is "not found", reopen your terminal (or `source ~/.zshrc`) so the new PATH takes effect.

---

## Step 1 — Get the code

```bash
git clone https://github.com/imran31415/kube-coder.git
cd kube-coder
```

Everything below is run from inside this directory.

---

## Step 2 — One command to bring it all up

```bash
make local
```

That's the whole deploy. Under the hood it runs five steps in order:

1. **`local-up`** — starts a minikube profile named `kube-coder` (Docker driver, 4 CPU / 6 GB) and enables the `ingress` addon (the same `ingress-nginx` the chart expects), then waits for the controller to be ready.
2. **`local-build`** — builds the workspace image as `kube-coder:local` **inside the minikube node** (no registry, no push). This is the slow part on first run.
3. **`local-secret`** — creates the `coder` namespace and a basic-auth secret for `admin` / `admin`.
4. **`local-deploy`** — installs the `base-infrastructure` chart and the `workspace` chart, then waits for the `ws-local` pod to roll out.
5. **`local-info`** — prints the final three steps to reach the dashboard.

Go make coffee — the first run can take 10–20 minutes depending on your machine and connection.

> Each step is its own target too, so if you tweak code later you can just re-run `make local-build && make local-deploy` instead of the whole thing.

---

## Step 3 — Map the hostname (one time)

The ingress routes by hostname, so point `kube-coder.local` at your loopback address. This needs `sudo` once:

```bash
echo '127.0.0.1  kube-coder.local' | sudo tee -a /etc/hosts
```

(You only ever do this once per machine.)

---

## Step 4 — Port-forward the ingress

With the Docker driver on macOS the minikube node IP isn't routable from the host, so you reach the cluster through a port-forward. **Keep this running in its own terminal tab** — it blocks:

```bash
make local-forward
```

This forwards `http://kube-coder.local:8080` → the in-cluster ingress controller. Leave it open; `Ctrl-C` stops it.

---

## Step 5 — Open the dashboard

In your browser:

```
http://kube-coder.local:8080/
```

Log in with basic auth:

- **Username:** `admin`
- **Password:** `admin`

You're in. 🎉

> Forgot the access details? Run `make local-info` any time to reprint the `/etc/hosts` line, URL, and credentials.

---

## Step 6 — Look around

- **Build** — create a Claude Code or OpenCode session. Pick an assistant + working directory, name it, hit **Start build**, and you land in a live terminal. Type your first prompt right in the REPL.
- **Terminal** — a persistent tmux terminal in the browser.
- **VS Code** — full code-server IDE at `/home/dev`.
- **Memory / Triggers / Files / Settings** — persistent memory store, webhooks/crons, the workspace filesystem, and metrics/health.

(On Apple Silicon the dashboard may open on the **Desktop** tab, which stays blank — just click **Build** or **Terminal**.)

### Optional: wire up an AI assistant

The workspace boots with no AI keys. Two options:

- **Interactive:** open a terminal in the dashboard and run `claude` once — it'll walk you through login.
- **Baked in:** drop a gitignored overlay at `secrets/local/keys.yaml`:
  ```yaml
  claude:
    apiKey: "sk-ant-..."
  assistant:
    openrouter:
      apiKey: "sk-or-..."
  ```
  then add `-f secrets/local/keys.yaml` to the deploy and re-run `make local-deploy`.

---

## Everyday commands

```bash
make local-info                          # reprint access details
make local-forward                       # (re)start the port-forward
make local-build && make local-deploy    # rebuild image + redeploy after a change

# Direct cluster access — always pinned to the kube-coder context
kubectl --context kube-coder -n coder get pods
kubectl --context kube-coder -n coder logs -f deploy/ws-local -c ide
kubectl --context kube-coder -n coder exec -it deploy/ws-local -c ide -- bash
```

### Tear it down

```bash
make local-down            # remove the workspace, keep the cluster
make local-down DELETE=1   # also delete the minikube profile entirely
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ErrImageNeverPull` / `ImagePullBackOff` on `ws-local` | Image didn't load — re-run `make local-build`. |
| `kube-coder.local` won't resolve | Add the `/etc/hosts` line and make sure `make local-forward` is running. |
| 401 / auth prompt loops | Creds are `admin` / `admin`. Re-run `make local-secret` if you changed them. |
| Pod stuck `ContainerCreating` | Usually the PVC or base infra — confirm `make local-deploy` installed `base-infrastructure`. |
| minikube won't start / OOMs | Raise Docker Desktop memory, or lower minikube's: `minikube start -p kube-coder --memory=4g`. |
| `make local` hangs on build | First build is genuinely slow (~2–3 GB image). Give it time; watch with `kubectl --context kube-coder -n coder get pods -w`. |

Full local docs: https://github.com/imran31415/kube-coder/blob/main/docs/local-development.md

---

That's it — a full cloud-style dev workspace running entirely on your MacBook. Once you're comfortable locally, the same chart deploys multi-tenant on a real cluster with GitHub OAuth and TLS — see the [Kubernetes deployment guide](deploy-on-kubernetes.md). Questions/issues → open an issue on the repo.
</content>
</invoke>
