# Dex signup implementation and validation

Validated locally on 2026-11-19. Public signup remains disabled by default.

## Implemented

- GitHub-only Dex chart with two replicas, Kubernetes storage, namespace-scoped
  RBAC, private mutually authenticated gRPC, NetworkPolicy and disruption budget.
- Separate signup listener, numeric GitHub ID eligibility, CSRF protection,
  persistent owner/slug reservations, immutable credentials and leased workers.
- Per-workspace Dex clients, owner-pinned OIDC, bounded requests, safe errors,
  structured backend diagnostics, persisted progress, retries and deletion cleanup.
- Reused GitOps/provisioner flow, with Jobs pinned to their published commit.
- Explicit admin migration preserving existing values and original credentials.
  Interrupted migration retains its mode; shared-namespace/custom-host migrations
  require operator preparation. Existing workspaces remain unchanged by default.
- Responsive signup page with sign-in, eligibility explanation, progress,
  reload recovery, short errors, retry actions and a ready workspace link.
- Shared wildcard certificate support and a DNS-01 certificate example.
- CI authentication, management and signup browser tests; deployment/runbook in
  [DEX_SIGNUP.md](../DEX_SIGNUP.md).

## Passed checks

| Check | Result |
|---|---|
| Controller Python suite, including signup, real Git checkouts/publishing and Helm render checks | 191 tests passed |
| Controller frontend unit suite | 70 tests passed |
| Workspace observability regression suite | 10 tests passed |
| Controller Helm unit suite | 95 tests passed |
| Workspace Helm unit suite | 328 tests passed |
| Frontend production build | Passed |
| Dex chart and enabled controller chart lint | Passed |
| Shared observability synchronization, shell syntax, diff whitespace | Passed |
| Real Dex v2.44.0 and pinned oauth2-proxy browser authentication | Owner accepted; non-owner callback 403; foreign workspace cookie and forged headers denied; refresh succeeds; denied HTTP/upgrade probes never reach upstream |
| Real Dex management via production Python adapter | mTLS works; missing client certificate denied; create/replay, conflict rejection and repeated deletion pass |
| Helm-packaged signup UI plus real Python HTTP/state machine | Signed-out and unapproved states, duplicate clicks, readiness, reload recovery, deletion/recreation, failure/retry/cooldown, ownership conflicts, lost accepted response, outage recovery, session expiry, no admin API calls, and mobile layout pass |

Revalidated after rebasing onto current `main` before opening the PR. On Windows,
the Python suite uses `PYTHONUTF8=1` because an upstream dashboard test reads
Unicode source files using the interpreter default encoding.

## Follow-up wiring audit

Traced the frontend build into Helm's ConfigMap and mounted runtime path; backend
module packaging; signup Service, proxy, environment variables and NetworkPolicy;
Dex mTLS certificate paths; GitOps values/secrets and provisioner inputs; admin
creation/migration and public signup routes.

Fixed issues found by the expanded tests/review:

- Web builds now populate Helm's `web-dist` automatically. The browser fixture
  reads the rendered ConfigMap rather than bypassing it with Vite output.
  Production signup fails clearly if the built signup UI is missing.
- A missing provisioner Job now retries deployment instead of remaining stuck
  in readiness. Failed Jobs use a fresh attempt; lost responses reuse the attempt.
- Full chart commit SHAs use fetch/checkout rather than `git clone -b`, which
  accepts branch/tag names. Tests use real repositories with newer commits to
  prove the requested old SHA/tag and GitOps commit are selected exactly.
- GitOps output uses UTF-8 explicitly. Local Git publishing and migration tests
  verify replay, preservation of customized values/old credentials, and that
  merged Helm output contains the intended issuer and numeric owner restriction.
- Admin creation follows the returned slug, preserving navigation after a GitHub
  rename. Non-retryable operation failures do not offer a misleading retry action.
- The signup transport handles pre-cancelled requests and malformed upstream
  error bodies with meaningful messages.

Additional backend tests exercise CSRF bound to cookie and identity, foreign
origins, malformed/oversized/forged bodies, revoked eligibility, retry cooldowns,
state-save loss, and two-observation deletion without treating API errors as
absence. These tests cover the listed scenarios; they are not a claim that every
possible failure or production deployment has been tested.

The signup browser test replaces GitHub, GitOps, Kubernetes and Dex management
with local fixtures. The authentication test uses real Dex/proxy containers with
local password identities and signed OIDC tokens. It does not exercise GitHub's
connector. The management test uses generated, short-lived fixture certificates.
Mobile screenshot inspected at 390×844; no horizontal overflow.

Tests caught and prompted fixes for lease release, Windows UTF-8 Helm output,
test cleanup, Dex's consent step, interrupted migration mode, overlay ordering,
and preservation of the exact configuration commit deployed by a Job.

## Deferred release checks

The user does not currently have the real OAuth App, two GitHub accounts, DNS
provider/domain or test cluster and requested deferral. Consequently these are
not claimed as tested:

- Actual GitHub connector login and owner/non-owner access through cluster ingress.
- Kubernetes provisioning, restart recovery, migration/rollback and deletion.
- CNI enforcement, gRPC isolation and failover with two replicas on separate nodes.
- DNS-01 wildcard issuance, renewal and ingress certificate selection.
- Production image builds/releases and deployment against those immutable artifacts.

Build updated controller and provisioner images and pin the corresponding chart
release before deployment. Complete the runbook's infrastructure checks before
enabling public signup. No live infrastructure was deployed or real credentials
used during this implementation.
