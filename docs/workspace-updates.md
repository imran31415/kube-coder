# Workspace version updates

Workspaces run a pinned image tag (`registry.../coder:devlaptop-v<X.Y.Z>`). When a
newer release exists, kube-coder can repoint a workspace at it — patching the live
Deployment (an immediate rollout) and, when GitOps is configured, committing the new
tag to the user's `values.yaml` so the change survives the next reconcile.

There are two ways to trigger it:

- **Admin** — from the **controller dashboard**, for any workspace. Always on; no
  extra setup.
- **Self-serve** — a user updates **their own** workspace from the workspace
  dashboard's **Settings → Updates** section. **Opt-in** (a token Secret;
  each workspace gets a token bound to it alone).

"Latest" comes from the **GitHub Releases** of `RELEASE_REPO` (default
`imran31415/kube-coder`): a release tagged `v1.5.0` maps to image tag
`devlaptop-v1.5.0`. The lookup is cached and best-effort — if GitHub is
unreachable the dashboard simply shows no update info rather than erroring.

## How each piece ships

This matters for what you need to redeploy (and is the one gotcha when testing):

| Piece | Ships via | To update |
|-------|-----------|-----------|
| Controller backend (`controller.py`) | ConfigMap | redeploy the controller chart |
| Controller dashboard (badge + Updates card) | `web-dist/` ConfigMap | `make controller-web` + redeploy |
| Workspace `server.py` broker | `browser-config` ConfigMap | `helm upgrade` + pod restart |
| **Workspace dashboard** (Settings → Updates) | **baked into the image** | needs a **new image** (a release built with this feature) |

The last row is the catch: a workspace only shows the **Settings → Updates** section
once it runs an image that was built with the feature in it. The admin flow has no
such constraint — the controller reads the workspace's Deployment image tag directly,
so it works against any workspace regardless of what that workspace runs.

## Security model

The controller's **admin API** trusts the `X-Forwarded-User` header injected by its
oauth2-proxy, so its NetworkPolicy deliberately pins ingress to the oauth2-proxy
alone — otherwise any pod in the namespace could forge that header and gain admin
over every workspace.

Self-serve must not weaken that, so it runs on a **separate, restricted listener**
(`SELF_SERVE_PORT`, default `8081`) that:

- trusts **only** the self-serve token (`X-KC-Service-Token`), never an identity header, and
- **404s every admin route** — only `/api/self/...` is served there.

The controller NetworkPolicy opens workspace pods to **8081 only**; the admin port
(`8080`) stays oauth2-only. A workspace can self-update but can never reach — let
alone forge an admin header against — the admin API.

**Tokens are per-workspace.** The Secret the operator creates holds the *master*
token, which only the controller reads. Each workspace namespace is seeded with
the derivation `HMAC-SHA256(master, "kc-self-serve/<user>")` instead (by
`scripts/ensure-workspace-namespace.sh`, which `make deploy` and the provisioner
run). The controller recomputes that HMAC for the username in the request path,
so a token read out of Alice's pod authorizes `/api/self/workspaces/alice/*`
only — presenting it while naming another workspace in the path is rejected.
The blast radius of a leaked workspace token is therefore "restart/update **that
workspace** to a release version" (data preserved), not any other workspace and
not admin control.

**Migration from the shared token.** Deployments seeded before per-workspace
derivation projected the master verbatim into every workspace. The controller
still accepts the master by default (`SELF_SERVE_ALLOW_SHARED_TOKEN`, values key
`controller.update.allowSharedToken`) and logs each such request with the
workspace that used it. To finish the migration: upgrade the controller first,
re-seed every workspace namespace (`make deploy USER=<u>` or re-run
`ensure-workspace-namespace.sh`), then set
`controller.update.allowSharedToken=false` so only bound tokens are accepted.

Neither path needs new RBAC: the image patch and a GitOps commit are the
already-granted `deployments[patch]` / a git push.

## Enabling self-serve

Self-serve is **off by default** (admins can still update any workspace from the
controller dashboard). To turn it on:

1. Create the master-token Secret in the controller's namespace, with key
   `self-serve-token` (only the controller ever reads this value):

   ```bash
   kubectl -n coder create secret generic kc-self-serve \
     --from-literal=self-serve-token="$(openssl rand -hex 32)"
   ```

   Workspace namespaces get their own per-workspace derived token automatically
   when `ensure-workspace-namespace.sh` runs (part of `make deploy USER=<u>` and
   of provisioning). To seed one by hand:

   ```bash
   MASTER=$(kubectl -n coder get secret kc-self-serve -o jsonpath='{.data.self-serve-token}' | base64 -d)
   DERIVED=$(KC_MASTER="$MASTER" KC_USER=<user> python3 -c 'import hashlib,hmac,os
   print(hmac.new(os.environ["KC_MASTER"].encode(),("kc-self-serve/"+os.environ["KC_USER"]).encode(),hashlib.sha256).hexdigest())')
   kubectl -n ws-<user> create secret generic kc-self-serve \
     --from-literal=self-serve-token="$DERIVED"
   ```

2. Name that Secret in **both** charts and redeploy:

   - controller: `controller.update.selfServeSecretName=kc-self-serve`
   - workspace:  `update.selfServeSecretName=kc-self-serve`

   (Set them in your values files / per-user values, or pass `--set` on
   `helm upgrade`.)

3. Confirm the controller started the restricted listener:

   ```bash
   kubectl -n coder logs deploy/workspace-controller | grep "self-serve listening"
   # → [controller] self-serve listening on 0.0.0.0:8081 (per-workspace tokens)
   ```

   With the Secret unset you'll instead see `self-serve disabled (SELF_SERVE_TOKEN unset)`,
   no `8081` port/policy is rendered, and the workspace's Settings shows no Updates
   section — the opt-in default.

Other tunables (env on the controller): `RELEASE_REPO`, `RELEASE_CHECK_TTL`
(latest-release cache, default 600s), `SELF_SERVE_PORT`. On the workspace,
`update.controllerUrl` overrides the in-cluster controller URL (defaults to
`http://workspace-controller.<namespace>.svc.cluster.local:8081`).

## Validating the whole flow after deploy

### A. Admin flow (test this first — no Secret, no new image)

1. Deploy the merged controller:

   ```bash
   make controller-web && make deploy-controller   # or helm upgrade the controller chart
   ```

2. Make a test workspace look out-of-date — set its image to any tag older than the
   latest release:

   ```bash
   kubectl -n coder set image deploy/ws-<user> \
     ide=registry.digitalocean.com/resourceloop/coder:devlaptop-v1.3.0
   ```

3. Open the **controller dashboard**. The workspace row shows a blue **`update`**
   badge and its current version; the detail view (`#/w/<user>`) shows an **Updates**
   card with **Restart & update**.

4. Click it and confirm. Watch the rollout:

   ```bash
   kubectl -n coder rollout status deploy/ws-<user>
   kubectl -n coder get deploy ws-<user> \
     -o jsonpath='{.spec.template.spec.containers[?(@.name=="ide")].image}{"\n"}'
   # → ...:devlaptop-v<latest>
   ```

5. If GitOps provisioning is configured, confirm a new commit landed in
   `users-private/<user>/values.yaml` in the GitOps repo (the persistence path).

### B. Self-serve flow (needs the Secret + a workspace on a feature-built image)

1. Enable self-serve (see above) and redeploy both charts.

2. Open the workspace dashboard → **Settings → Updates**. It shows the current vs
   latest version. When a newer release exists, click **Restart & update**, confirm,
   and the pod restarts onto the latest image.

   > To see an *available* update in self-serve you need `latest > current` while the
   > workspace is on a feature-built image. Since downgrading the image would also drop
   > the Updates section (it's baked into the image), the natural way is: run the
   > workspace on release **N**, then cut a small follow-up release **N+1** — the
   > workspace then shows the badge. (Alternatively, point the controller's
   > `RELEASE_REPO` at a fork whose latest release tag is higher.)

### C. Connectivity + boundary sanity (optional)

From inside the workspace pod — the broker can reach the self-serve port (the
token is the pod's own per-workspace one, from `$CONTROLLER_SELF_SERVE_TOKEN`;
it only works for this pod's own `<user>` in the path):

```bash
curl -s -H "X-KC-Service-Token: $CONTROLLER_SELF_SERVE_TOKEN" \
  http://workspace-controller.coder.svc.cluster.local:8081/api/self/workspaces/<user>/version
# → {"user":"<user>","version":"v1.4.0","latestVersion":"v1.5.0","updateAvailable":true,...}
```

…and the admin port must **not** be reachable from the workspace (NetworkPolicy):

```bash
curl --max-time 5 http://workspace-controller.coder.svc.cluster.local:8080/api/workspaces
# → connection refused / times out
```

## Endpoint reference

| Method & path | Port | Auth | Purpose |
|---------------|------|------|---------|
| `GET /api/workspaces` | 8080 | oauth2 (admin) | list incl. `version`, `updateAvailable`, `latestVersion` |
| `POST /api/workspaces/<user>/update` | 8080 | oauth2 (admin) | update any workspace (body: optional `{"version":"v1.5.0"}`) |
| `GET /api/self/workspaces/<user>/version` | 8081 | workspace-bound token | current vs latest for one workspace |
| `POST /api/self/workspaces/<user>/update` | 8081 | workspace-bound token | self-update (brokered by the workspace backend) |

On the workspace side the dashboard calls its own backend, which brokers to the
controller: `GET /api/workspace/version` and `POST /api/workspace/update`.
