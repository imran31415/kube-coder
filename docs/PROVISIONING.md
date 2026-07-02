# Self-service workspace provisioning

The **workspace-controller** dashboard can provision a brand-new workspace from
a GitHub username: it registers a GitHub App for the user, writes the rendered
config to a private GitOps repo, and runs `helm upgrade` — turning the manual
[New User Provisioning](NEW_USER_PROVISIONING.md) checklist into a form
submission.

This is **off by default** and self-hostable: every endpoint is your cluster,
your GitOps repo, your GitHub Apps. Nothing is shared with the kube-coder
project. This guide is the end-to-end setup.

> **Per-workspace namespaces (#103):** each provisioned workspace lands in its
> own `ws-<user>` namespace with scoped RBAC + a `ResourceQuota`/`LimitRange`.
> The provisioner creates and seeds that namespace (regcred, base-infra) for
> you. Migrating pre-existing `coder`-resident workspaces? See
> [Per-workspace namespace migration](PER_WORKSPACE_NAMESPACE_MIGRATION.md).

---

## How it works

```
Admin → controller "New workspace" → types a GitHub username
  1. controller validates the username against the GitHub API
  2. browser POSTs a GitHub App *manifest* to github.com
  3. admin clicks "Create GitHub App"            ← the one manual click
  4. GitHub redirects back with a one-time code; the controller exchanges it
     for the App's client_id + client_secret
  5. controller renders values.yaml + secrets/oauth2.yaml and pushes them to
     YOUR private GitOps repo
  6. controller launches a short-lived, privileged Job
  7. the Job clones the chart + your config repo and runs `make deploy`
  8. the UI polls until the workspace pod is running
```

**Why GitHub Apps and not OAuth Apps?** GitHub has no API to create classic
OAuth Apps — only GitHub Apps can be registered programmatically (via the
manifest flow). A GitHub App exposes the same `client_id`/`client_secret` login
that `oauth2-proxy --provider=github` already uses, so the workspace auth is
unchanged; you just click "Create" once per user instead of filling a form.

**Security posture.** The always-on, internet-facing controller never holds
write power over workspaces. It can only validate input, do the manifest
exchange, push to git, and *create a Job*. The Job runs as a separate
`workspace-provisioner` ServiceAccount that holds the create/update/delete RBAC
— so a controller compromise can start Jobs, not silently rewrite every tenant's
Deployment or read every Secret.

---

## Prerequisites

| Need | Why |
|---|---|
| A deployed workspace-controller | The console this feature lives in (see its [README](../charts/workspace-controller/README.md)). |
| **Wildcard DNS** `*.<domain>` → ingress | New workspaces are served at `<login>.<domain>` with no per-host DNS step. |
| A **private** Git repo | Stores generated `values.yaml` + secrets (the GitOps source of truth). |
| A **push token** for that repo | The controller + Job push/clone it. Fine-grained PAT (Contents: RW) or a GitHub App installation token. |
| Admin can create GitHub Apps | Under their own account, or an org you control (`provision.githubOrg`). |

---

## Setup

### 1. Run the scaffold

```sh
scripts/setup-controller-provisioning.sh \
  --domain dev.example.io \
  --gitops-repo github.com/<you>/kube-coder-users.git \
  [--github-org <org>] \
  [--chart-repo https://github.com/<you>/kube-coder.git]
```

This generates the `state-secret`, writes the gitignored
`users-private/_controller/secrets/provision.yaml`, and prints the next steps.

### 2. Create the private GitOps repo

It needs an initial commit on its default branch (an empty repo can't be cloned):

```sh
gh repo create <you>/kube-coder-users --private --add-readme --clone=false
```

Layout the controller writes (you don't create these by hand):

```
kube-coder-users/                 # private
└── users-private/
    └── <login>/
        ├── values.yaml
        └── secrets/
            └── oauth2.yaml
```

### 3. Add the push token

Create a token that can **push to the GitOps repo** and **read the GitHub API**,
and paste it into the `gitToken` field of
`users-private/_controller/secrets/provision.yaml`.

- Simplest: a [fine-grained PAT](https://github.com/settings/personal-access-tokens)
  scoped to just the GitOps repo, **Contents: Read and write**.
- Or a GitHub App installation token if you already run one.

### 4. Enable it in the controller values

Merge the printed block into `users-private/_controller/values.yaml`:

```yaml
provision:
  enabled: true
  workspaceDomain: dev.example.io
  gitops:
    repo: github.com/<you>/kube-coder-users.git
    branch: main
  # githubOrg: acme          # omit to create Apps under the admin's account
  # chart:                   # omit to use the upstream kube-coder chart
  #   repo: https://github.com/<you>/kube-coder.git
  #   ref: main
```

### 5. Deploy

```sh
make ship-controller-config     # build SPA → helm upgrade → roll the pod
```

The chart now also creates the `workspace-controller-provision` Secret (from
your `provision.yaml` overlay), the `workspace-provisioner` ServiceAccount +
Role, and grants the controller `batch/jobs:create`. With `provision.enabled`
false, none of these exist and the controller stays a least-privileged
list/scale console.

### 6. Use it

Open `https://<controller-host>`, click **New workspace**, type a username, and
follow the one GitHub confirmation click.

---

## Hardening (recommended)

Generated config — including the App's **client secret** and the workspace
**cookie secret** — is committed to the GitOps repo. Keep the repo private. To
avoid plaintext secrets in git, manage the controller's provisioning Secret
yourself (sealed-secrets / SOPS / external-secrets) and point at it:

```yaml
provision:
  existingSecretName: my-provision-secret   # keys: git-token, state-secret
```

then leave `provision.gitToken` / `provision.stateSecret` empty.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "New workspace" button missing | `provision.enabled` false, or required values unset → `/api/provision/config` reports `enabled:false`. |
| Validation says "github lookup failed" | `gitToken` can't read the GitHub API, or the username doesn't exist. |
| Stuck on "Starting provisioner" | Inspect the Job (runs in the control-plane namespace): `kubectl -n coder logs job/provision-<slug>-<ts>`. |
| Job fails cloning the GitOps repo | Token lacks push/clone access, or the repo has no initial commit on `branch`. |
| Workspace pod never ready | Chart deploy issue — same as a manual `make deploy`; the workspace lives in its OWN namespace (#103): `kubectl -n ws-<slug> describe deploy ws-<slug>`. |
| Provision Job forbidden creating the namespace | The provisioner needs its cluster-scoped grants — redeploy the controller chart so the `workspace-provisioner` ClusterRole/ClusterRoleBinding exist. |
| Limit edits revert after a redeploy | Expected: in-place edits are live `kubectl patch` (like start/stop); durable changes go in the workspace `values.yaml`. |

The provisioner Job runs the same `make deploy USER=<slug>` you would run by
hand, against config in your GitOps repo — so anything that works manually works
here, and vice-versa.
