# Self-service workspace provisioning

The admin dashboard provisions workspaces using a GitHub username and manually
created GitHub OAuth App credentials. Public signup is an optional Dex mode:
one GitHub OAuth App connects to Dex, and approved numeric GitHub IDs can create
their own workspace. See [Dex signup setup and migration](DEX_SIGNUP.md).

Provisioning is off by default. The controller writes private GitOps configuration
and runs the existing provisioner Job. This guide covers the legacy admin setup.

> **Per-workspace namespaces (#103):** each provisioned workspace lands in its
> own `ws-<user>` namespace with scoped RBAC + a `ResourceQuota`/`LimitRange`.
> The provisioner creates and seeds that namespace (regcred, base-infra) for
> you. Migrating pre-existing `coder`-resident workspaces? See
> [Per-workspace namespace migration](PER_WORKSPACE_NAMESPACE_MIGRATION.md).

---

## How it works

1. An administrator looks up a GitHub username.
2. They create a GitHub **OAuth App** manually and enter its credentials.
3. The controller pushes workspace values and secrets to the private GitOps repo.
4. A provisioner Job clones the pinned chart and configuration, then runs `make deploy`.
5. The dashboard reports deployment status.

GitHub OAuth Apps cannot be created through an API. There is no GitHub App
manifest exchange in this flow. Dex mode removes the per-user registration step;
existing workspaces remain on their original provider until explicitly migrated.

The controller launches a separate privileged provisioner ServiceAccount. Its
ability to create Jobs is still security-sensitive; keep the pinned image and
admission policy enabled. Dex mode additionally grants access to a dedicated
signup state namespace and the private, mutually authenticated Dex API.

---

## Prerequisites

| Need | Why |
|---|---|
| A deployed workspace-controller | The console this feature lives in (see its [README](../charts/workspace-controller/README.md)). |
| **Wildcard DNS** `*.<domain>` → ingress | New workspaces are served at `<login>.<domain>` with no per-host DNS step. |
| A **private** Git repo | Stores generated `values.yaml` + secrets (the GitOps source of truth). |
| A **push token** for that repo | The controller + Job push/clone it. Fine-grained PAT (Contents: RW) or a GitHub App installation token. |
| Admin can create GitHub OAuth Apps | One per legacy workspace, or one shared App for Dex mode. |

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

## Deleting and recreating a workspace

`make delete-user USER=<slug>` is **cluster-only**. It removes the Helm release,
the home PVC and the `ws-<slug>` namespace, but deliberately leaves two things
standing: the GitHub OAuth App, and the workspace's config in the GitOps repo
(`users-private/<slug>/`).

That second one is load-bearing. `gitops_config_exists()` in `controller.py`
surfaces the saved config to the console as `configExists`, which makes the
**New workspace** flow skip the OAuth credential form entirely and deploy
straight from the stored `clientId`. That is the intended behaviour for
retrying a failed provision — but it also means a delete-and-recreate rebuilds
the workspace against the *same* OAuth App, silently.

So if you deleted or rotated the OAuth App, clear the saved config too:

```bash
make forget-user USER=<slug>    # drops users-private/<slug>/ from the GitOps repo and pushes
```

The next provision then prompts for a fresh Client ID and secret. Without it the
new workspace boots against dead credentials and login fails in a way that looks
identical to a misprovisioned workspace.

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
| Recreated workspace still fails to log in | The delete left `users-private/<slug>/` in the GitOps repo, so the console skipped the credential form (`configExists`) and redeployed the old `clientId`. Run `make forget-user USER=<slug>` and provision again. |
| Limit edits revert after a redeploy | Expected: in-place edits are live `kubectl patch` (like start/stop); durable changes go in the workspace `values.yaml`. |

The provisioner Job runs the same `make deploy USER=<slug>` you would run by
hand, against config in your GitOps repo — so anything that works manually works
here, and vice-versa.
