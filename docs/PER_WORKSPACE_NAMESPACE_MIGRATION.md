# Migrating workspaces to per-user namespaces (#103)

Historically every workspace was deployed into a single shared `coder`
namespace. As of the per-workspace-namespace change, each workspace lives in its
**own** `ws-<user>` namespace, with a scoped `ResourceQuota`/`LimitRange` and
truly tenant-scoped RBAC. This closes the cross-tenant Secret exposure that the
shared namespace allowed (a workspace SA could `get`/`delete` a co-tenant's
Secret by its predictable name) and makes egress/quota policy tractable.

This runbook covers moving **existing** tenants. New workspaces already land in
their own namespace — no action needed.

## What changed

| Before | After |
|--------|-------|
| All workspaces in `coder` | Each workspace in `ws-<user>` |
| Workspace SA could touch any Secret in `coder` by name | SA is alone in its namespace — no cross-tenant reach |
| Controller `Role` scoped to `coder` | Controller `ClusterRole`, **bound per-namespace** (no cluster-wide scale) + a read-only `namespaces` discovery grant |
| Provisioner `Role` in `coder` | Provisioner `ClusterRole` (must create namespaces) — still a short-lived Job, controller only gets `create jobs` |
| `regcred` + `base-infrastructure` once in `coder` | Replicated into each `ws-<user>` namespace (`make deploy` does this) |

The controller lists workspaces across every `ws-<user>` namespace (by name
convention) **and** the shared `coder` namespace, so a half-migrated fleet keeps
working — migrate tenants one at a time with no console downtime.

## Prerequisites

1. Deploy the updated **workspace-controller** chart first. It adds the
   `workspace-controller` ClusterRole, the read-only `namespaces` discovery
   ClusterRoleBinding, and (when provisioning is enabled) the provisioner
   ClusterRole/ClusterRoleBinding.

   ```bash
   make deploy-controller
   ```

2. Confirm the controller can still see the existing `coder`-resident
   workspaces in its dashboard (discovery falls back to `coder`).

## Migrate one tenant

A home PVC is namespace-scoped and **cannot be moved** — its data must be
copied. `scripts/migrate-user-namespace.sh` automates the safe parts (quiesce →
create namespace + regcred → new PVC → tar-pipe the home volume) and stops
before anything destructive.

```bash
# Dry-run first — prints every action without touching the cluster:
scripts/migrate-user-namespace.sh <user> --dry-run

# Then for real:
scripts/migrate-user-namespace.sh <user>
```

The script:

1. Scales `ws-<user>` in `coder` to 0 (so the home volume is quiescent).
2. Creates + labels the `ws-<user>` namespace and copies `regcred` into it.
3. Creates a new `ws-<user>-home` PVC (same size) in `ws-<user>`.
4. Streams `/home/dev` from the old PVC to the new one via two helper pods
   (`kubectl exec … tar -cf - | kubectl exec -i … tar -xf -`).

It then prints — but does **not** run — the cutover steps.

### Cutover (run deliberately, after the copy)

1. Point the tenant's config at the new namespace and deploy:

   ```bash
   # in the user's values.yaml (GitOps repo .users/users-private/<user>/ or
   # users-private/<user>/): change the namespace field to ws-<user>
   namespace: ws-<user>
   ```
   ```bash
   make deploy USER=<user>
   ```

   `make deploy` creates/labels the namespace (idempotent), copies `regcred`,
   installs `base-infrastructure` (the `kaniko-wrapper` ConfigMap the pod mounts)
   into `ws-<user>`, then rolls the workspace onto the migrated PVC.

2. Verify health and that the home directory survived:

   ```bash
   kubectl -n ws-<user> get pods
   kubectl -n ws-<user> exec deploy/ws-<user> -c ide -- ls -la /home/dev
   ```

3. Once satisfied, remove the old `coder` copy:

   ```bash
   helm uninstall <user>-workspace -n coder || true
   kubectl delete pvc ws-<user>-home -n coder
   ```

   Leave the shared `coder` objects (the controller, `base-infrastructure`,
   `regcred`) in place — `coder` remains the control-plane namespace.

4. Commit the `namespace: ws-<user>` change to the GitOps repo so the next
   reconcile is a no-op rather than reverting the move.

## Rollback

The migration never deletes the source until you run the cleanup in step 3, so
rollback before cleanup is simply:

```bash
kubectl scale deployment ws-<user> -n coder --replicas=1   # bring the old one back
kubectl delete namespace ws-<user>                          # discard the half-migrated copy
```

## Verifying isolation (acceptance)

From inside tenant **A**'s workspace, tenant **B**'s Secrets and pods must be
unreachable:

```bash
kubectl get secret github-app-secrets-<B> -n ws-<B>    # => Forbidden
kubectl get pods -n ws-<B>                             # => Forbidden
```

Both fail because A's ServiceAccount has no RoleBinding in `ws-<B>` — the
namespace boundary, not a name guess, is what stops the access.
