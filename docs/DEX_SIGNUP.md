# Dex signup and workspace authentication

Dex provisioning and public signup are opt-in. Existing workspaces keep GitHub
authentication until an administrator explicitly migrates them. Public signup
allows authentication by any GitHub user, but workspace creation is restricted
to `signup.allowedGithubIds`. An empty list permits no public creation.

## Components and boundaries

- `charts/dex`: GitHub-only Dex v2.44.0, two replicas, Kubernetes CRD storage,
  private mutually authenticated gRPC, and public HTTPS OIDC endpoints. Helm
  installs the CRDs; the running Dex service account has only namespaced access.
- Existing controller admin listener: port 8080, existing admin authentication.
- Signup listener: port 8082, a separate proxy and host. It has no admin routes.
- State namespace: owner/slug ConfigMaps, immutable credential Secrets, and a
  Lease coordinating reconciliation across controller replicas. Only the
  controller gets access. Include this namespace and Dex storage in backups.
- One confidential Dex client per GitHub numeric ID, with one exact callback.
  Each workspace proxy compares `federated_claims.user_id` against its owner ID.
  This configuration requires the GitHub-only Dex connector shipped here; do not
  add another connector with an overlapping user-ID namespace.

Numeric ownership survives GitHub username changes. The assigned workspace
hostname does not change automatically. Existing slugs remain reserved after
deletion, preventing username recycling from transferring saved configuration.
The creation allowlist controls provisioning, not revocation of existing access.

## One-time setup

These steps require infrastructure and real GitHub credentials. They have been
deferred for this implementation; do not enable public signup before validation.

1. Choose a workspace domain and separate Dex and signup hosts. Provision DNS,
   ingress-nginx and a NetworkPolicy-enforcing CNI. Keep the existing admin host.
2. Create one GitHub **OAuth App** for Dex. Its callback is
   `https://<dex-host>/callback` when the issuer has no path prefix. Workspace
   callbacks are registered with Dex automatically, not with GitHub.
3. In the Dex namespace, create the GitHub credential Secret (`client-id`,
   `client-secret`) and signup client Secret (`client-secret`). In the controller
   namespace, create the signup proxy Secret (`client-id=kube-coder-signup`,
   matching `client-secret`, random 32-byte/base64 `cookie-secret`, and a random
   `csrf-secret` of at least 32 characters). Never commit real credentials here.
4. Issue a gRPC server certificate with a SAN matching the service DNS name used
   in `dex.grpcAddress`, and a controller client certificate with clientAuth EKU.
   Dex's gRPC Secret contains its server `tls.crt`/`tls.key` and client CA
   `ca.crt`. The controller Secret contains its client `tls.crt`/`tls.key` and
   server CA `ca.crt`. Plan certificate renewal and restart the controller after
   rotation so its gRPC channel reloads credentials.
5. Deploy `charts/dex` into a dedicated namespace using its values file. Set
   issuer, host, credential Secret names, signup host and TLS configuration.
   Both replicas share Kubernetes storage; do not use SQLite in production.
6. Build/release the updated controller image (it adds `grpcio` and `protobuf`)
   and provisioner image (it checks out the published GitOps commit),
   and pin `provision.chart.ref` to the release/commit containing the OIDC chart.
   Keep the provisioner image pinned by digest. Upgrade image and chart together.
7. Run `make controller-web` before deploying the controller chart. The web build
   copies its single-file output into `web-dist/index.html`, which Helm packages
   into the mounted ConfigMap. Signup refuses to start with a placeholder or old
   bundle. Configure the controller overlay below, initially with signup disabled:

```yaml
dex:
  enabled: true
  issuerUrl: https://auth.workspaces.example.com
  grpcAddress: dex.dex.svc.cluster.local:5557
  clientTlsSecretName: dex-controller-client
  stateNamespace: kube-coder-signup
signup:
  enabled: false
  host: signup.workspaces.example.com
  allowedGithubIds: ["123456"] # numeric IDs as strings, never usernames
  existingSecretName: workspace-signup
  sharedCertificate: true
```

Existing `provision` configuration remains required: private GitOps repository,
push token, workspace domain, pinned chart ref and provisioner image. Secrets
continue to be published into that private repository following the existing
workflow. Protect repository access and backups accordingly.

Validate a workspace through the admin console, including a non-owner login,
before setting `signup.enabled: true`.

## Wildcard TLS

Use `deployments/dex-wildcard-certificate.yaml` as an example, replacing the domain
and DNS-01 ClusterIssuer. Configure ingress-nginx's default SSL certificate as
`ingress-nginx/workspaces-wildcard`. Set the workspace, signup and Dex
`sharedCertificate` switches only once that certificate is ready.

Shared mode omits per-ingress `secretName` and certificate issuance annotations.
The private key remains in the ingress namespace, with no tenant copies.
All hosts must be covered by the certificate; a wildcard covers only one label.
Legacy per-host certificates remain supported. DNS-provider setup, actual
issuance and renewal validation require the deployment owner's infrastructure.

## HTTP contracts

| Endpoint | Authorization and behavior |
|---|---|
| `GET /api/signup/config` | Public; enabled display state only |
| `GET /api/signup/me` | Verified proxy identity; eligibility, CSRF token and own operation |
| `POST /api/signup/workspace` | Approved ID, same origin, session-bound CSRF; empty JSON body; returns existing/new operation with 202 |
| `GET /api/signup/status` | Own persisted progress only |
| `POST /api/signup/retry` | Approved ID and CSRF; resumes retryable work with 202 |
| `DELETE /api/provision/<slug>/identity` | Existing admin boundary; requires workspace absence |

Signup rejects caller-supplied owner, host, image, resource settings or OAuth
credentials. The trusted signup proxy overwrites identity headers and maps the
verified federated user ID to `X-Forwarded-Groups`. NetworkPolicy isolates port
8082. Keep that proxy's public route allowlist limited to the landing page and
public configuration endpoint. OAuth login and logout use `/oauth2/*`.

Admin `/api/provision/config` exposes `authMode`. Dex-mode creation accepts
`{user}` or an explicit `{user, migrate: true}` migration. Legacy creation keeps
its existing credential-based request. Migration is not a public signup action.

## Recovery, deletion and migration

Provisioning stages are persisted before proceeding to the next stage. Client
IDs, credentials and Job names are reused after lost responses. Git publishing
re-clones on conflict; the worker serializes its external effects under a Lease.
A crashed worker's lease expires after ten minutes. The UI shows progress and
safe errors rather than automatically replaying a timed-out mutation.

`make delete-user` keeps its existing destructive confirmation. After cluster
teardown, it invokes `scripts/cleanup-dex-identity.py`. Set `KC_CONTROLLER_URL`
and `KC_CONTROLLER_ADMIN_TOKEN` for immediate controller-mediated cleanup.
Without them, the worker observes namespace absence twice, at least a minute
apart, before removing a completed managed workspace's Dex client. Kubernetes
errors never count as absence. Failed/incomplete provisions require admin
investigation; they are not automatically deleted. Client deletion alone does
not revoke an already-issued proxy cookie; teardown removes the proxy too.

To migrate an existing standard-host workspace, verify its owner with the
administrator and first move any shared-namespace workspace to its own
`ws-<slug>` namespace using the existing namespace migration guide. Then use
the admin Dex migration action. It creates
`secrets/zz-dex.yaml` as a final values overlay; existing resources, custom
settings and old credentials remain intact. The new cookie key forces login.
Review any custom overlay that sorts after `zz-dex.yaml` before migration.
Check owner access and non-owner 403 before retiring the old OAuth App.

Rollback: remove the Dex overlay from the private GitOps repo, redeploy the
previous configuration and verify login. Keep signup disabled during rollback.
Do not remove retained ownership records to give the same hostname to another
GitHub ID. Custom-host migrations require an operator-reviewed configuration.

## Diagnostics and tests

Backend JSON logs include request/error IDs, operation ID, stage, duration and
safe stack locations. Tokens, OAuth codes and raw upstream error bodies are not
logged. Existing controller metrics include bounded `signup_<stage>` operation
outcomes. Use the UI's support reference to find the corresponding log event.

```sh
python -m unittest discover -s charts/workspace-controller/tests -p '*_test.py'
yarn --cwd charts/workspace-controller/web build
yarn --cwd charts/workspace-controller/web test
helm lint charts/dex -f charts/dex/tests/test-values.yaml
```

The container authentication suite uses the real pinned Dex/proxy images and
local fixture identities. Its HTTP/password configuration is test-only:

```sh
docker compose -p kc-dex-test -f charts/workspace-controller/tests/integration/compose.yaml up -d
node charts/workspace/web/scripts/dex-auth-e2e.mjs
docker compose -p kc-dex-test -f charts/workspace-controller/tests/integration/compose.yaml down
```

Install the existing workspace web dependencies and Playwright Chromium first.
The test checks owner access, non-owner 403, refresh, isolation between workspace
cookies, and that denied HTTP/upgrade requests do not reach the upstream.

For the mTLS management test, run `tests/integration/generate_certs.py` with
Python (requires OpenSSL), then add `-f tests/integration/compose-grpc.yaml` to
the compose command above, using paths relative to `charts/workspace-controller`.
Run `tests/integration/dex_management.py` in an isolated environment containing
the pinned `grpcio` and `protobuf` versions from `devlaptop/requirements.txt`.
The generated two-day certificates stay in the ignored `.certs` directory.
This checks creation, replay, conflict protection, deletion and rejection of
callers without client certificates.

For the full signup browser flow, build the controller web app, start
`charts/workspace-controller/tests/integration/signup_fixture.py`, and run
`node charts/workspace/web/scripts/signup-e2e.mjs`. This uses the actual UI,
HTTP handler and provisioning state machine with fixture infrastructure. CI
runs both browser suites and the mTLS test.

Real GitHub login, actual Kubernetes provisioning/HA/network-policy enforcement,
and production DNS/TLS issuance and renewal are separate release checks. They
remain deferred until the required accounts and infrastructure are available.
