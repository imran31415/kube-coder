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
  - The GitHub App private key is mounted **only** on the
    `github-app-token` sidecar, never on the `ide` container where the
    agent runs (issue #558). Everything in `ide` — including every
    process the agent spawns, which inherits its environment — sees only
    the installation token, which expires in an hour. Treat any change
    that puts a long-lived credential back into the `ide` environment as
    a regression: an agent can be induced to read its own environment by
    an injected instruction file or a package lifecycle hook.

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
| `networkPolicy.egress.enabled` | `true` (restricted) | `false` lets anything running in the pod reach the cloud metadata endpoint (`169.254.169.254`, which serves node bootstrap credentials) and other tenants' pod IPs. Set the denied ranges to your cluster's pod/service CIDRs rather than disabling. |
| `ALLOW_INTERNAL_HOOKS` | `false` | Setting `true` lets task completion-hooks POST to RFC1918 / metadata IPs. |
| `TRUSTED_PROXY` | `true` | Set `false` for any deploy where the ingress doesn't strip client-supplied auth headers. |

See `charts/workspace/values.yaml` for full annotations.

## Optional: sandboxed container runtimes (gVisor / Kata)

The table above lists defaults you can loosen. `runtimeClassName` is the
opposite — a default you can tighten, for operators who have already invested in
sandboxed nodes:

```yaml
runtimeClassName: gvisor        # or kata-containers, or any RuntimeClass name
```

A container normally shares the host's kernel, and the isolation is namespaces
and cgroups. gVisor intercepts syscalls in a user-space kernel; Kata runs the pod
in a lightweight VM. Either bounds what a compromised agent reaches. Empty (the
default) omits the field entirely and keeps today's behaviour exactly.

**Prerequisite, and what happens without it.** The cluster must *already* have
nodes running that runtime **and** a matching `RuntimeClass` object. Naming one
that does not exist makes the workspace pod **unschedulable** — it sits `Pending`
and the workspace never starts. kube-coder does not install runtimes or create
RuntimeClass objects; whether your cloud offers such node pools is a question for
your provider.

**`build.mode: buildkit` and gVisor do not mix.** That mode adds a
`privileged: true` DinD sidecar, and gVisor does not support privileged
containers, so builds fail — at build time rather than at schedule time, which
makes it awkward to diagnose. Kata is VM-backed and does not have this
limitation. The default `kaniko` mode has no DinD sidecar and is unaffected.
`runtimeClassName` is pod-level, so there is no way to sandbox the agent
container and leave the build sidecar on the default runtime.

**What this does not do.** It does not make kube-coder secure-by-default against
a hostile agent, and it does nothing for prompt injection. It also does not
separate a workspace's human from its agents: every agent in a pod shares the
same kernel boundary regardless of which runtime that pod uses.
