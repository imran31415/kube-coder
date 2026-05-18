# Security policy

## Reporting a vulnerability

If you believe you've found a security vulnerability in kube-coder, please
**do not file a public GitHub issue**. Instead, email a description of the
issue to:

**security@imranresearch.dev** (or open a private security advisory at
https://github.com/imran31415/kube-coder/security/advisories/new)

We aim to acknowledge receipt within 72 hours and follow up with a fix or
disposition within two weeks for routine issues. For severe issues that
expose user data or allow tenant escape we will move faster.

Please include:

- A description of the issue and its impact (what an attacker can do).
- Reproduction steps or proof-of-concept against a fresh `make new-user` deploy.
- Affected commit / chart version.
- Any suggested mitigation.

## Scope

kube-coder ships infrastructure that provisions per-user Kubernetes
workspaces. The security boundaries we care about:

- **Authentication** — the OAuth2 / basic-auth gates on the dashboard,
  the Bearer-token auth on the Claude task API, and the public-readonly
  demo mode (`ingress.auth.type=none` + `readOnly: true`).
- **Multi-tenant isolation** — one workspace pod must not be able to
  read, modify, or impersonate another tenant's data. The shared `coder`
  namespace + per-workspace ServiceAccount model is the current trust
  boundary; per-workspace namespaces are the long-term hardening target.
- **Code execution & SSRF surfaces** — the Claude task API spawns
  shell sessions; the completion-hook posts arbitrary URLs; the file
  upload accepts binary bodies. All three are explicit attack surfaces.
- **Secret exposure** — the operator's GitHub App private key, OAuth2
  client secret, registry pull secret, and SSH authorized_keys all live
  in Kubernetes Secrets accessible from the workspace.

Out of scope:

- Self-DoS via the user filling their own PVC, fork-bombing their own
  shell, or exhausting their own resource quota.
- Issues that require an attacker to already have a shell on the
  workspace pod *and* a different tenant's namespace+secret name. Per-
  workspace namespaces are the planned fix; in the meantime cross-
  tenant reads via known names are a documented limitation.

## Hardening defaults

The repo ships secure defaults; deployments that flip these on take
responsibility for the corresponding risk:

| Setting | Default | Loosening means |
|---|---|---|
| `readOnly` | `false` (writable) | Set with `auth.type=none` only — server.py refuses otherwise. |
| `ingress.auth.type` | `basic` | `none` exposes the dashboard to the internet (requires `readOnly: true`). |
| `build.mode` | `kaniko` | `buildkit` adds a privileged DinD sidecar (container-escape surface). |
| `ALLOW_INTERNAL_HOOKS` | `false` | Setting `true` lets task completion-hooks POST to RFC1918 / metadata IPs. |
| `TRUSTED_PROXY` | `true` | Set `false` for any deploy where the ingress doesn't strip client-supplied auth headers. |

See `charts/workspace/values.yaml` for full annotations.
