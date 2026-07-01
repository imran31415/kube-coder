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

## The easy path: `make migrate-user` / `make migrate-all`

A home PVC is namespace-scoped and **cannot be moved** — its data must be
copied. The migration is wrapped in two Make targets so you rarely touch the
script directly. Everything is **reversible until the final decommission step**,
and each phase is opt-in via a flag.

```bash
# Always dry-run first — prints every action, touches nothing:
make migrate-user USER=<name> CUTOVER=1 DRY_RUN=1

# Copy only (safe to run ahead of time; source stays up-to-0 for rollback):
make migrate-user USER=<name>

# Copy + cut over (repoint values.yaml → ws-<name>, deploy into it, verify):
make migrate-user USER=<name> CUTOVER=1

# The whole thing, including reclaiming the old copy (destructive last step):
make migrate-user USER=<name> DECOMMISSION=1
```

Migrate the **entire fleet** at once — it discovers every `ws-*` Deployment in
the source namespace (default `coder`) and runs the same phases for each:

```bash
make migrate-all DRY_RUN=1 CUTOVER=1     # preview the whole fleet
make migrate-all CUTOVER=1               # copy + cut over every workspace
make migrate-all DECOMMISSION=1          # …and reclaim the old copies
```

Check progress at any time (read-only) — which workspaces are already in their
own namespace vs still in the shared source namespace:

```bash
make migrate-status
```
```
=== workspace namespace migration status (source: coder) ===
  USER                   NAMESPACE                STATUS
  alice                  coder                    PENDING (shared coder)
  bob                    ws-bob                   migrated

  1 migrated / 1 pending / 2 total
```

(A workspace mid-migration — copied but not yet decommissioned — shows on both
rows: `ws-<user>` migrated and `coder` pending, until you decommission the old.)

### The three phases (what each flag does)

| Phase | Flag | Actions | Reversible? |
|-------|------|---------|-------------|
| **Copy** | *(default)* | Scale source to 0 → create+label `ws-<user>` ns + copy `regcred` → new `ws-<user>-home` PVC → tar-pipe `/home/dev` across namespaces | ✅ source untouched |
| **Cutover** | `CUTOVER=1` | Set `namespace: ws-<user>` in the user's values.yaml → `make deploy USER=<user>` (installs base-infra into `ws-<user>`, rolls the pod onto the migrated PVC) → verify `/home/dev` | ✅ old copy left scaled-to-0 |
| **Decommission** | `DECOMMISSION=1` | `helm uninstall` + delete the old `ws-<user>-home` PVC in the source namespace | ❌ destructive |

The tenant's only downtime is the copy window (source is scaled to 0 while
`/home/dev` streams between two helper pods). `coder` stays as the control-plane
namespace — the controller, `base-infrastructure`, and `regcred` live there
permanently; only the per-user workspaces relocate.

> **Commit the GitOps change.** `CUTOVER` edits the user's `values.yaml`
> `namespace:` field in your resolved config dir (`deployments/`,
> `users-private/`, or the `.users/` GitOps checkout). Commit + push that change
> so the next reconcile is a no-op rather than reverting the move.

### Doing it by hand (equivalent to the phases above)

```bash
scripts/migrate-user-namespace.sh <user>              # copy
# then set namespace: ws-<user> in the values.yaml and:
make deploy USER=<user>                               # cutover
kubectl -n ws-<user> exec deploy/ws-<user> -c ide -- ls -la /home/dev   # verify
helm uninstall <user>-workspace -n coder || true      # decommission
kubectl delete pvc ws-<user>-home -n coder
```

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
