# Platform observability

kube-coder exposes process metrics and writes application JSON logs to stderr.
The cluster operator owns Prometheus, log collection, retention, Alertmanager
routes and notification delivery. No collector or notification service is
installed by these charts. Monitors and alert rules are disabled by default.

## Enable collection

Prerequisites, all operator-owned:

- The Prometheus Operator CRDs (`PodMonitor`, and `PrometheusRule` for alerts).
  The charts fail rendering when a requested monitor or rule CRD is missing.
- A Prometheus whose `podMonitorSelector` matches the monitor labels you choose
  below and whose `podMonitorNamespaceSelector` includes the controller
  namespace **and** every workspace namespace. Alert-rule labels must match its
  `ruleSelector`. Check with
  `kubectl get prometheus -A -o jsonpath='{..podMonitorSelector}'`.
- A CNI that enforces NetworkPolicy, or the policies below are decorative.
- For the alert rules: kube-state-metrics and kubelet/cAdvisor scrapes.

### 1. Controller

Controller values (replace the namespace and labels for your cluster):

```yaml
controller:
  metricsScrapeToken:
    enabled: true          # creates the master Secret kc-metrics-scrape here
  metrics:
    enabled: true          # metrics-only listener, GET /metrics on 9091
    port: 9091
    prometheusNamespace: monitoring
    prometheusPodLabels:
      app.kubernetes.io/name: prometheus
    podMonitor:
      enabled: true
      labels:
        release: prometheus
    prometheusRule:
      enabled: false       # see "Optional alerts" before turning on
      workspaceScrapeAlert: false
      labels:
        release: prometheus
```

`metricsScrapeToken.enabled` creates the **master** Secret `kc-metrics-scrape`
(key `metrics-scrape-token`) in the controller namespace. It is generated once
and preserved across upgrades; pin it with `metricsScrapeToken.value`, or bring
your own Secret with `metricsScrapeToken.existingSecretName`. The master never
enters a tenant namespace.

### 2. Each workspace

After the master Secret exists, run the namespace provisioning script for the
workspace. It derives that workspace's own token,
HMAC-SHA256(master, `kc-metrics-scrape/<user>`), into a Secret of the same name
in the workspace namespace:

```bash
scripts/ensure-workspace-namespace.sh ws-alice alice <controller-namespace>
kubectl -n ws-alice get secret kc-metrics-scrape   # must exist before the next step
```

If you used `existingSecretName`, set `METRICS_SCRAPE_SECRET_NAME` to that name
when running the script and use the same name as `metrics.scrapeSecretName`.

Then deploy the workspace with:

```yaml
metrics:
  scrapeSecretName: kc-metrics-scrape   # mounts KC_METRICS_SCRAPE_TOKEN and starts the listener
  port: 6082                            # default; any free port except 6080/6081/7681/8080
  prometheusNamespace: monitoring
  prometheusPodLabels:
    app.kubernetes.io/name: prometheus
  podMonitor:
    enabled: true
    labels:
      release: prometheus
```

`scrapeSecretName` starts a **metrics-only listener** in the workspace pod on
`metrics.port` (container port name `metrics`). It serves `GET
/metrics/prometheus` and nothing else: any other path is 404, any other method
405, and it accepts only that workspace's scrape token (not the Claude Task API
token). The PodMonitor scrapes port `metrics` with the token from the local
Secret, and the workspace NetworkPolicy admits the configured Prometheus pods
to that one port.

Port 6080 is never opened to Prometheus. It is the dashboard and Task API, and
in oauth2 mode it trusts `X-Auth-Request-User`/`Remote-User` from whatever
reaches it directly, so a monitoring pod admitted there could forge a header
and drive the workspace. 6080's own `/metrics/prometheus` still answers the
Task API token for ad-hoc reads from inside the pod, and refuses the scrape
token.

### Security model

The controller serves only `GET /metrics` on port 9091. It is a separate handler
from the proxy-trusting admin API on 8080 and the self-serve API on 8081, and
its NetworkPolicy admits only the configured monitoring namespace **and** pod
selector, on 9091. The workspace listener works the same way, plus the bearer
token. Never publish either metrics port through a public ingress; neither is
on a Service the ingress routes to.

Rotation requires reseeding every workspace Secret and restarting its pod so
the environment token and Prometheus credential agree: delete the master
Secret (or set a new `value`), upgrade the controller, re-run the provisioning
script for every workspace, then restart each workspace. The token is read on
every request, so a restarted pod never accepts the withdrawn one.

### 3. Verify

```bash
# The workspace listener: 401 without the token, metrics with it, nothing else.
T=$(kubectl -n ws-alice get secret kc-metrics-scrape -o jsonpath='{.data.metrics-scrape-token}' | base64 -d)
kubectl -n ws-alice port-forward deploy/ws-alice 6082:6082 &
curl -s -o /dev/null -w '%{http_code}\n' localhost:6082/metrics/prometheus                        # 401
curl -s -H "Authorization: Bearer $T" localhost:6082/metrics/prometheus | head                   # # HELP kubecoder_...
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" localhost:6082/api/claude/tasks  # 404

# The Prometheus rule names the metrics port and never 6080.
kubectl -n ws-alice get networkpolicy ws-alice-ingress -o jsonpath='{.spec.ingress[*].ports}'

# The controller listener.
kubectl -n <controller-namespace> port-forward deploy/workspace-controller 9091:9091 &
curl -s localhost:9091/metrics | head
```

In Prometheus, `up{kube_coder_component="workspace"}` and
`up{kube_coder_component="controller"}` should be 1 for every running
workspace and every controller replica.

## What the metrics mean

| Metric | Meaning |
| --- | --- |
| `kubecoder_http_requests_total` | Final responses by service, listener, bounded route, method and status class (`2xx`..`5xx`) |
| `kubecoder_http_request_duration_seconds` | Ordinary request latency histogram; excludes SSE and proxy lifetimes |
| `kubecoder_http_requests_in_flight` | Active ordinary requests, streams and proxies |
| `kubecoder_http_aborted_requests_total` | Disconnects observed at the HTTP boundary |
| `kubecoder_controller_operations_total` | Scale, update, resource-change and provisioning-submission outcomes |
| `kubecoder_controller_operation_duration_seconds` | Time spent in those controller operations |

These counters reset on process restart. Sum their **rates** across replicas;
do not treat one replica as the fleet total. Routes never include usernames,
query strings or request IDs. Routes are RouteTable handler names or fixed
controller templates (`/api/workspaces/:user/...`); every other path is
`route="other"`, and unknown methods are `method="other"`. A controller `submitted` outcome means a Job was submitted, not that
provisioning completed. `partial` means an operation reported a persistence or
reconciliation failure. There is no background reconcile loop to instrument.

Existing disk-derived product gauges retain their semantics: pruning stored
tasks can lower a gauge. Upload quota gauges describe uploads, not the whole PVC.
`kubecoder_metrics_collector_up` reports section collection failures separately
from zero usage. HTTP telemetry is process-local and is not a durable audit log.

Example fleet latency query:

```promql
histogram_quantile(0.95,
  sum by (le, route) (rate(kubecoder_http_request_duration_seconds_bucket[5m])))
```

## Dashboard failure and recovery

Capacity, spend, insights, health and workspace metrics reuse one polling hook.
Only one read runs at a time. Switching workspace or range cancels the old read,
and a late response cannot replace the new selection. Reads have a browser
deadline; controller metrics reads also share a backend budget across upstream
queries. Mutations are never retried automatically.

Transient failures retain the last good reading with an explicit previous-data
label and timestamp. A response that reports a problem but still carries data
(for example kubectl figures while Prometheus is down) shows that fresh data with
the problem, not an older reading. Initial failures show a short reason instead
of zero usage or a healthy status; the insights bar first retries quietly every
2 s, up to 3 times, because the controller's first reads after a restart can fail
transiently. Retry is available; repeated failures back off. Hidden tabs pause
polling. Missing monitoring configuration or no reporting workspaces offer setup
guidance plus **Check again**, and are re-checked every 5 minutes, so the panel
recovers on its own once workspaces start reporting. An expired oauth2 session
(the browser's fetch error on the identity provider redirect) sends the page to
sign in, as the workspace dashboard does.

Responses preserve existing fields and add a `problem` object with `code`, safe
`error`, `retryable`, `request_id` and `error_id`. Operator-facing failures
(kubectl, GitHub, provisioning) keep their actionable `error` text in the
console; the same text is logged under the `error_id`. Expected states
(`metrics_disabled`, `metrics_not_reporting`) have no `error_id` and are not
logged, since every open tab reports them on every poll; their `request_id`
still matches the request's `request_completed` event. The dashboard's **Copy reference**
action lets an operator find the matching backend event. Every observed response
also includes `X-Request-ID`, generated by the server rather than trusted from a
client header.

## Debugging logs

Collect stderr JSON from the controller and workspace containers using your
existing cluster log agent. Events contain timestamp, service, level and event.
Request completion includes the normalized route, status and duration. Failures
include correlation IDs where available, exception type plus stack locations,
and the operator-facing reason as `detail`. Messages and `detail` pass through
redaction: bearer tokens and `token=`/`password=`/`secret=`/`api_key=` values are
replaced, and URLs keep scheme, host and path but lose credentials, query string
and fragment. Raw exception objects and source lines are never serialized.
Prometheus failures log their real reason as `prometheus_query_failed` at most
once a minute, with a `suppressed` count.
Existing diagnostics are migrated through a small logging adapter; new events
should use named, allowlisted fields rather than interpolate request bodies.
Auxiliary tools and sidecars retain their own logging formats.

Search by the copied `error_id` or `request_id`. Check the exception type and
file/function/line, then the relevant upstream service. Keep log access restricted
to operators. Never add authorization headers, credentials, raw upstream bodies
or query strings to a structured event.

## Optional alerts and runbook

Enable `controller.metrics.prometheusRule.enabled` only after the targets report
healthy data. Rules require kube-state-metrics, kubelet/cAdvisor and workspace
scrapes. The default tenant namespace regex is `ws-.*`; override it if needed.

`WorkspaceMetricsUnavailable` fires on *absence*: every running workspace with
no healthy scrape. Workspace scraping is set per workspace in the workspace
chart (`metrics.podMonitor.enabled`, off by default), which this chart cannot
see, so that rule has its own switch,
`controller.metrics.prometheusRule.workspaceScrapeAlert` (default `false`).
Turn it on only once every workspace is scraped; otherwise it fires for every
running workspace. The other rules stay silent without workspace scrapes.

| Alert | Default trigger | First action |
| --- | --- | --- |
| WorkspaceMemoryPressure | Working set exceeds 90% of a positive container limit for 5m | Inspect memory consumers and restart history; resize only after diagnosis |
| WorkspaceDiskPressure, warning | PVC usage above 85% for 15m | Inspect large files and plan cleanup or expansion |
| WorkspaceDiskPressure, critical | PVC usage above 95% for 5m | Free safe-to-remove data or expand the PVC promptly |
| WorkspaceHookDeadLetters | Dead letters remain for 5m | Check destination and credentials before retrying delivery |
| WorkspaceMetricsUnavailable (needs `workspaceScrapeAlert`) | Running workspace has no healthy scrape for 10m | Check target selection, local scrape Secret and NetworkPolicy |
| WorkspaceMetricsCollectorFailed | A collector section reports zero for 5m | Inspect workspace logs and that section's backing data |

Sleeping workspaces have no running pod and do not trigger the missing-scrape
rule. Duplicate kubelet targets are aggregated before division. Zero or absent
limits do not create a false memory ratio. Missing source metrics are not zero:
check source health when a pressure rule has no data. Both disk severities can
fire; configure Alertmanager inhibition to suppress warning when critical fires.
Notification receivers and routes remain the operator's responsibility.

## Validation and rollback

Run `python scripts/sync-observability.py --check` to verify that controller
ConfigMap helpers match their canonical workspace files. After changing helpers,
run the script without `--check` and commit both copies. No runtime packages are
required. Run `python scripts/check-observability-rules.py` with Helm and promtool
on PATH to test rendered rule syntax, firing windows, recovery, duplicate series,
missing targets and sleeping workspaces, for both the default rule set and the
one with `workspaceScrapeAlert` (where unscraped workspaces must stay silent
and missing-scrape alerts must fire, respectively).

Before production rollout, verify two controller replicas appear as separate
targets, tenant tokens cannot authorize task APIs, monitoring pods cannot reach
the controller's 8080/8081 or a workspace's 6080, and a deliberate staging
scrape failure triggers and resolves the expected alert. Test the operator's notification route separately. Disable
PodMonitors and rules before disabling metrics or removing scrape Secrets.
Application JSON logging needs no additional writable volume.
