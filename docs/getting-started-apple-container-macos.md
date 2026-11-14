# Run kube-coder on a Mac with Apple's `container` (no Docker Desktop, no minikube)

This guide runs a kube-coder workspace on a Mac using
[Apple's `container`](https://github.com/apple/container) instead of Docker
Desktop + minikube. `container cluster` starts a local Kubernetes cluster
(kind's `kindest/node`) in a lightweight VM, and `container build` builds the
workspace image straight into an image store that cluster reads. The Helm chart
and the `make local-*` targets are the same ones the
[minikube guide](getting-started-minikube-macos.md) uses. The only difference
is `LOCAL_DRIVER=apple`.

> **Status: experimental (#879).** This path was written against
> `container` 1.7.1's documentation and has **not yet been run end to end on
> real hardware**. If something below does not match what you see, the
> [smoke checklist](#smoke-checklist) and [open questions](#open-questions)
> list what to check. Please report what you find on the issue.
> `container cluster` is itself marked experimental by Apple.

## Requirements

- A Mac with **Apple silicon** (M1 or newer). Intel Macs can't run `container`.
  Use the [minikube guide](getting-started-minikube-macos.md) instead.
- **macOS 26** or newer.
- About **4 CPUs / 6 GB of RAM** free for the cluster, and ~10 GB of disk for
  the image and the workspace volume.

## Step 0: install the tools

```bash
brew install container kubectl helm
```

(Or install `container` from the signed `.pkg` on the
[releases page](https://github.com/apple/container/releases).) Then start its
system service once per login:

```bash
container system start
```

`make local-up` also starts it if it isn't running. You do **not** need
Docker Desktop or minikube.

## Step 1: get the code

```bash
git clone https://github.com/imran31415/kube-coder.git
cd kube-coder
```

## Step 2: bring it up

```bash
make local LOCAL_DRIVER=apple
```

That runs, in order:

| Target | With `LOCAL_DRIVER=apple` |
|---|---|
| `local-up` | `container cluster create --name kube-coder --cpus 4 --memory 6g --shared-image-store`. This writes a `kube-coder` context into `~/.kube/config`. Then it installs a default StorageClass if there isn't one, applies ingress-nginx's **kind** manifest (kind has no ingress addon) and metrics-server with `--kubelet-insecure-tls`, and waits for the ingress controller. |
| `local-build` | `container build -t kube-coder:local -f devlaptop/Dockerfile .`, a native arm64 build into your Mac's image store. `--shared-image-store` lets the cluster run it with no registry. |
| `local-secret` | the `ws-local` namespace and the `admin`/`admin` basic-auth secret (same as minikube). |
| `local-deploy` | the same Helm releases, plus `--set image.pullPolicy=Always` (see [Rebuilds](#rebuilds-and-pull-policy)). |
| `local-info` | prints the next steps. |

The first build takes a while because the image is ~2–3 GB. Later builds use
the cache.

Every `kubectl`/`helm` call names `--context kube-coder` explicitly, so none
of this can touch another cluster.

> **Tip:** to avoid typing the driver on every command, run
> `export LOCAL_DRIVER=apple` in your shell. The targets read it from the
> environment.

## Step 3: reach the dashboard

Same as the minikube path:

```bash
echo '127.0.0.1  kube-coder.local' | sudo tee -a /etc/hosts   # one time
make local-forward LOCAL_DRIVER=apple                         # keep running
open http://kube-coder.local:8080/                            # admin / admin
```

## Everyday commands

```bash
make local-build local-deploy LOCAL_DRIVER=apple   # rebuild the image + redeploy
make local-info LOCAL_DRIVER=apple                 # reprint access details
kubectl --context kube-coder -n ws-local get pods
kubectl --context kube-coder -n ws-local logs -f deploy/ws-local -c ide
container cluster list                             # cluster state
```

The always-on keeper (#728):

```bash
make keeper-build-local LOCAL_DRIVER=apple
make local-deploy KEEPER=1 LOCAL_DRIVER=apple
```

NetworkPolicy that is actually enforced (kind's default CNI, kindnet, ignores
it, just like minikube's):

```bash
make local-down DELETE=1 LOCAL_DRIVER=apple
make local-up LOCAL_CNI=calico LOCAL_DRIVER=apple
```

With the apple driver, `LOCAL_CNI=calico` downloads a pinned Calico manifest
and passes it to `container cluster create --cni`. You can also pass a path to
any plain CNI manifest: `LOCAL_CNI=./my-cni.yaml`. It must be a
`kubectl apply`-able YAML, not a Helm chart.

### Rebuilds and pull policy

The local overlay uses `image.pullPolicy: IfNotPresent`. With
`--shared-image-store`, Apple's docs warn that a node **keeps the image it
already has** under `IfNotPresent` when you rebuild the same tag. A rebuilt
`kube-coder:local` would then be silently ignored after `rollout restart`. The
apple driver therefore deploys with `image.pullPolicy=Always` (and
`keeper.imagePullPolicy=Always`). The node checks the Mac's image store first
for every pull, so this still never reaches a registry.

## Tear it down

```bash
make local-down LOCAL_DRIVER=apple             # remove the workspace, keep the cluster
make local-down DELETE=1 LOCAL_DRIVER=apple    # also delete the container cluster
```

> ⚠️ **A stopped cluster cannot be restarted. Back up first.** Apple's
> documented recovery for a cluster whose control-plane node has stopped
> (after `container system stop`, a reboot, or a crash) is
> `container cluster delete` followed by `create`. That **destroys the
> workspace's persistent volume** (`/home/dev`). Before you stop the container
> system or reboot, copy out anything you care about, for example:
>
> ```bash
> kubectl --context kube-coder -n ws-local exec deploy/ws-local -c ide -- \
>   tar czf - -C /home/dev . > kube-coder-home-backup.tgz
> ```
>
> Push your git work to a remote as usual. See also
> [BACKUP_RESTORE.md](BACKUP_RESTORE.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ERROR: Apple 'container' not found` | `brew install container`, then `container system start`. |
| `needs macOS on Apple silicon` | `container` only runs on Apple silicon with macOS 26. Use `LOCAL_DRIVER=minikube`. |
| `no kubeconfig context 'kube-coder'` | Check `container cluster list` and `kubectl config get-contexts`. The context should be named after `--name`. If yours is different, please report it on #879. |
| `ws-local` stuck in `ImagePullBackOff` for `kube-coder:local` | The node didn't find the image in the local store and fell back to Docker Hub. Make sure `make local-build LOCAL_DRIVER=apple` finished and `container image list` shows `kube-coder:local`. The cluster must also have been **created** with `--shared-image-store` (`make local-up` does this). That setting can't be added to an existing cluster, so delete and re-create it. |
| `container cluster list` shows `stopped` | See the warning in [Tear it down](#tear-it-down): `make local-down DELETE=1 LOCAL_DRIVER=apple && make local LOCAL_DRIVER=apple`. |
| PVC stuck `Pending` | `kubectl --context kube-coder get storageclass` should show one `(default)`. `local-up` installs local-path-provisioner if none is marked default. |
| Desktop / noVNC tab is blank | Known on arm64: the bundled Firefox is x86_64. Everything else works. Use the other tabs. |

## Smoke checklist

Run this on a real Mac before calling the apple driver supported:

- [ ] `make local LOCAL_DRIVER=apple` completes on a clean machine
- [ ] Dashboard loads through `make local-forward`, and basic auth works
- [ ] Terminal tab opens a shell
- [ ] code-server loads
- [ ] A Claude Build (task) starts and streams output
- [ ] Memory and Files tabs work
- [ ] Edit something in `/home/dev`, then `kubectl --context kube-coder -n ws-local rollout restart deploy/ws-local`. The file survives (PVC).
- [ ] Rebuild loop: change something in the image, `make local-build local-deploy LOCAL_DRIVER=apple`, and confirm the new pod runs the **new** image
- [ ] `make keeper-build-local` + `make local-deploy KEEPER=1` (both with `LOCAL_DRIVER=apple`): the keeper pod runs, and `kubectl top` works (metrics-server)
- [ ] `LOCAL_CNI=calico`: a NetworkPolicy really blocks traffic
- [ ] `make local-down DELETE=1 LOCAL_DRIVER=apple` removes the cluster and the `kube-coder` kubeconfig context

## Open questions

These were unknown when the driver was written (#879):

1. Does the `kindest/node` cluster ship the `standard` local-path StorageClass
   as default? `local-up` installs local-path-provisioner as a fallback either
   way.
2. Does `build.mode=buildkit` work? It runs a **privileged `dind` sidecar**
   inside a kind node inside an Apple VM. The local overlay uses Kaniko mode,
   which doesn't need it.
3. Does ingress-nginx's kind manifest come up without kind's
   `extraPortMappings`? Its hostPorts aren't reachable from the Mac either
   way, which is why access goes through `make local-forward`.
4. Can a cluster survive `container system stop` or a reboot? See the backup
   warning above.
