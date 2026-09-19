# Provisioning broker — topology, upgrade, and how to verify it

**Issue:** [#421](https://github.com/imran31415/kube-coder/issues/421).
**Supersedes:** the interim mitigations in
[#416](https://github.com/imran31415/kube-coder/issues/416) (admission policy)
and [#420](https://github.com/imran31415/kube-coder/issues/420) (immutable chart
refs) — both stay, as defense-in-depth *behind* this architecture rather than
in place of it.

## What changed, in one paragraph

Self-service provisioning needs cluster-wide create verbs. Until #421 those
lived on a `workspace-provisioner` ServiceAccount **in the controller's own
namespace**, and the internet-facing controller held `create jobs` there. In
Kubernetes, a principal that can create a workload and name another SA beside it
inherits that SA's identity — the kubelet mounts a token for whatever SA the
manifest names. So "the controller can start Jobs" silently meant "the
controller can become the provisioner" (July 2026 security review, finding 4).
The privileged half now lives in its own namespace, and the controller's only
grant there is `create` on a custom resource whose entire spec is one string.

## Topology

```
  control-plane namespace (e.g. coder)          broker namespace (kube-coder-provision)
  ┌────────────────────────────────┐            ┌──────────────────────────────────────┐
  │ workspace-controller  (Deploy) │            │ workspace-provision-broker  (Deploy) │
  │  SA: workspace-controller      │            │  SA: workspace-provision-broker      │
  │                                │            │   • get/list/watch/delete  PVRs      │
  │  in THIS namespace:            │            │   • patch PVR /status                │
  │   • deployments r/scale/patch  │            │   • create/delete Jobs (this ns)     │
  │   • pods/pvc/ingress read      │            │                                      │
  │  in the BROKER namespace:      │  create    │            stamps ↓ (own template)   │
  │   • create/get/list/watch      │ ─────────► │ ProvisionRequest ──► provision Job   │
  │     provisionrequests          │  (API      │                        SA: workspace-│
  │   • NOTHING ELSE               │   server   │                            provisioner
  └────────────────────────────────┘   is the   └──────────────────────────────────────┘
                                       auth
                                       boundary)
```

Files:

| Concern | File |
|---|---|
| Request schema (one field) | `charts/workspace-controller/templates/provisionrequest-crd.yaml` |
| Broker reconciler | `charts/workspace-controller/broker.py` |
| Broker namespace | `templates/broker-namespace.yaml` |
| Broker RBAC + the controller's grant | `templates/broker-rbac.yaml` |
| Privileged SA (now in the broker ns) | `templates/provisioner-rbac.yaml` |
| Admission guard (scoped to the broker ns) | `templates/provisioner-vap.yaml` |
| Controller side | `controller.py` :: `build_provision_request` / `provision_status` |

## Why a CRD and not a broker HTTP API

The API server *is* the authentication boundary. A CRD gives you authn, authz,
audit logging, admission, schema validation and RBAC-per-verb for free. An
internal HTTP endpoint would need a shared secret or mTLS, a NetworkPolicy, and
its own request validation — more moving parts on the most privileged path in
the system, for no gain.

## Why the request cannot smuggle anything

The CRD's `openAPIV3Schema` is **structural** and declares exactly one property
under `spec`. Kubernetes therefore **prunes** every unknown field on write:
`spec.image`, `spec.command`, `spec.serviceAccountName` do not survive being
persisted, so the broker never sees them.

Be precise about the mechanism: structural schemas *prune*, they do not
*reject*. (`additionalProperties: false` cannot be used alongside `properties`
in a structural schema, so pruning is the enforcement.) The security outcome is
identical — the field is gone — but an attacker gets a silent drop, not an
error. `broker.py` additionally re-validates `spec.slug` against the same
pattern the CRD pins, so a schema regression cannot widen what reaches a shell.

## Upgrade path (existing installs)

Order matters, because the ClusterRoleBinding's subject namespace changes.

1. **Roll chart + controller image together.** The controller no longer receives
   `CHART_REF`/`PROVISIONER_IMAGE`; a new controller against an old chart cannot
   provision, and an old controller against the new chart would try to create a
   Job it is no longer permitted to create. Both fail closed (no silent
   half-provision), but the outage is avoidable by rolling together.
2. `helm upgrade --install` as usual. The chart creates the broker namespace,
   the CRD, the broker, and moves the `workspace-provisioner` SA.
3. **Copy `regcred` into the broker namespace** — the one step the chart cannot
   do, since it holds no registry credentials and a pod can only pull from a
   Secret in its own namespace:

   ```bash
   scripts/ensure-provision-broker-namespace.sh            # defaults: kube-coder-provision, coder
   scripts/ensure-provision-broker-namespace.sh my-ns my-control-plane-ns
   ```

   Until this runs the broker pod sits in `ImagePullBackOff` and recovers by
   itself once the Secret lands.
4. **In-flight provisions do not migrate.** A Job running in the old namespace
   completes on its own; its `provisionUser`-labelled Job is simply no longer
   what `provision_status` reads. Re-run the provision through the console if a
   workspace was mid-creation during the upgrade.
5. Old `workspace-controller-provision` Role/RoleBinding in the control-plane
   namespace are removed by Helm as deleted templates. Verify with step 2 of the
   matrix below.

## Verifying it on a live cluster

The chart tests (`helm unittest charts/workspace-controller`) assert the RBAC
*manifests*. Only the API server can tell you what those manifests actually
authorize, so run this matrix after upgrading. `CTRL` is the controller SA,
`BNS` the broker namespace, `CNS` the control-plane namespace.

```bash
CNS=coder; BNS=kube-coder-provision
CTRL="system:serviceaccount:${CNS}:workspace-controller"
BROKER="system:serviceaccount:${BNS}:workspace-provision-broker"

# --- must be NO (the four acceptance criteria that are denials) -------------
kubectl auth can-i create jobs            --as "$CTRL" -n "$BNS"   # AC 2
kubectl auth can-i create jobs            --as "$CTRL" -n "$CNS"   # AC 1 (Role deleted)
kubectl auth can-i create deployments     --as "$CTRL" -n "$BNS"   # AC 2
kubectl auth can-i patch  provisionrequests/status --as "$CTRL" -n "$BNS"
kubectl auth can-i update provisionrequests --as "$CTRL" -n "$BNS"
kubectl auth can-i get    secrets         --as "$CTRL" -n "$BNS"
kubectl auth can-i create namespaces      --as "$BROKER"           # broker is not privileged

# --- must be YES (the path still works) ------------------------------------
kubectl auth can-i create provisionrequests --as "$CTRL"   -n "$BNS"   # AC 5
kubectl auth can-i get    provisionrequests --as "$CTRL"   -n "$BNS"
kubectl auth can-i create jobs              --as "$BROKER" -n "$BNS"
kubectl auth can-i patch  provisionrequests/status --as "$BROKER" -n "$BNS"
```

AC 1 has a second half worth checking directly — that the controller cannot
create a Job *selecting the provisioner SA* anywhere. Since the SA exists only
in `$BNS` and the controller has no batch verb there, the first two denials
cover it; if you want the belt-and-braces version, attempt the create:

```bash
kubectl --as "$CTRL" -n "$BNS" create -f - <<'YAML'
apiVersion: batch/v1
kind: Job
metadata: { name: escalation-probe }
spec:
  template:
    spec:
      serviceAccountName: workspace-provisioner
      restartPolicy: Never
      containers: [{ name: c, image: busybox, command: ["sh","-c","id"] }]
YAML
# expected: Error from server (Forbidden): ... cannot create resource "jobs"
```

Then prove the constrained path still provisions end to end (AC 5):

```bash
kubectl -n "$BNS" get provisionrequests           # the console's create should appear here
kubectl -n "$BNS" get pvr -w                      # Pending -> Running -> Succeeded
kubectl -n "$BNS" logs job/<request-name>         # the privileged Job's own output
```

And AC 6 — disabling provisioning removes the privileged identity entirely:

```bash
helm upgrade ... --set provision.enabled=false
kubectl get clusterrole workspace-provisioner            # NotFound
kubectl get clusterrolebinding workspace-provisioner     # NotFound
kubectl get ns "$BNS"                                    # NotFound (chart-owned)
```

## Operating the broker

- **Status lives on the request**, not in logs: `kubectl -n $BNS get pvr` shows
  `Slug / Phase / Job / Age`. A `Failed` phase carries the reason in
  `.status.message` — including misconfiguration the controller can no longer
  see for itself, such as an unset `provision.image` or a mutable chart ref.
- **Finished requests are retired** after `provision.broker.retainHours` (24 by
  default). Their Jobs are garbage-collected with them via `ownerReferences`.
- **Stamping is idempotent**: the Job takes the request's name, so a broker that
  crashes between creating the Job and recording it hits `AlreadyExists` on the
  next pass rather than starting a second privileged `helm upgrade`.
- **One replica, `Recreate`.** Two brokers would race on status transitions.

## Known limits / still open on #421

- **Per-tenant scoped provisioners.** Still one cluster-wide provisioner
  identity. Scoping it per tenant needs the `ws-<user>` namespace to pre-exist,
  since RBAC cannot grant `create` into a namespace that does not exist yet.
- **Splitting the controller's public API from its GitOps writer.** The
  controller still holds the GitOps push token and renders the per-user values;
  only the *Kubernetes* half moved behind the broker.
- **Namespace `delete`** remains off the normal path (teardown is a manual admin
  runbook), unchanged by this work.
