{{/*
DECIDED NOT TO IMPLEMENT (kept for context):

We considered stripping client-supplied identity headers
(X-Forwarded-User / -Email / -Preferred-Username / -Groups) at each
workspace-facing Ingress as defense-in-depth, in case some future
in-pod code starts trusting them. The natural mechanism on
nginx-ingress is `nginx.ingress.kubernetes.io/configuration-snippet`,
but the cluster's ingress controller runs with `--allow-snippet-
annotations=false` (CVE-2021-25742 hardening, cluster-wide) and its
admission webhook rejects snippets.

Re-enabling snippets would weaken posture for 20+ unrelated tenants on
this shared DOKS cluster — not worth it for a header-strip that
nothing currently trusts. If a future in-pod handler begins
authorizing off these headers, revisit by adding a single shared
ConfigMap in `ingress-nginx` and pointing each workspace ingress at it
via the `proxy-set-headers` annotation (which is *not* a snippet and
is allowed).
*/}}
