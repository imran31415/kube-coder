# Platform observability

kube-coder exposes process metrics and writes application JSON logs to stderr.
The cluster operator owns Prometheus, log collection, retention, Alertmanager
routes and notification delivery. No collector or notification service is
installed by these charts. Monitors and alert rules are disabled by default.

## Enable collection

Install the Prometheus Operator CRDs first. Choose monitor labels that match
your Prometheus `podMonitorSelector`, and ensure its namespace selector includes
both the controller namespace and the workspace namespaces. Alert-rule labels
must match its `ruleSelector` too. The chart fails rendering when a requested
monitor or rule CRD is missing.

Controller values (replace the namespace and release label for your cluster):

```yaml
controller:
  metricsScrapeToken:
    enabled: true
  metrics:
    enabled: true
    port: 9091
    prometheusNamespace: monitoring
    prometheusPodLabels:
      app.kubernetes.io/name: prometheus
    podMonitor:
      enabled: true
      labels:
        release: prometheus
    prometheusRule:
      enabled: false
      labels:
        release: prometheus
```

For each workspace, run the existing namespace provisioning script after the
master scrape Secret exists, then deploy these workspace values:

```yaml
metrics:
  scrapeSecretName: kc-metrics-scrape
  prometheusNamespace: monitoring
  prometheusPodLabels:
    app.kubernetes.io/name: prometheus
  podMonitor:
    enabled: true
    labels:
      release: prometheus
```

`scripts/ensure-workspace-namespace.sh` derives a separate HMAC token for each
workspace from the controller namespace's master Secret. The master never enters
a tenant namespace. The workspace PodMonitor references its local Secret, and
the pod reads that same Secret through `KC_METRICS_SCRAPE_TOKEN`. An externally
managed Secret can be selected with `controller.metricsScrapeToken.existingSecretName`;
set `METRICS_SCRAPE_SECRET_NAME` to that name when running the provisioning script
and use it as the workspace `metrics.scrapeSecretName` as well.

Rotation requires reseeding every workspace Secret and restarting its pod so
the environment token and Prometheus credential agree. Existing workspace task
API credentials remain compatible, but give Prometheus only the metrics token.
The metrics token does not authorize any task or mutation endpoint.

The controller serves only `GET /metrics` on port 9091. It is a separate handler
from the proxy-trusting admin API on 8080 and the self-serve API on 8081. Never
publish the metrics listener through a public ingress. Its NetworkPolicy admits
only the configured monitoring namespace **and** pod selector. Workspace metrics
remain at `GET /metrics/prometheus` on port 6080 with bearer authentication.
NetworkPolicy enforcement requires a supporting CNI.

## What the metrics mean

| Metric | Meaning |
| --- | --- |
| `kubecoder_http_requests_total` | Final responses by service, listener, bounded route, method and status |
| `kubecoder_http_request_duration_seconds` | Ordinary request latency histogram; excludes SSE and proxy lifetimes |
| `kubecoder_http_requests_in_flight` | Active ordinary requests, streams and proxies |
| `kubecoder_http_aborted_requests_total` | Disconnects observed at the HTTP boundary |
| `kubecoder_controller_operations_total` | Scale, update, resource-change and provisioning-submission outcomes |
| `kubecoder_controller_operation_duration_seconds` | Time spent in those controller operations |

These counters reset on process restart. Sum their **rates** across replicas;
do not treat one replica as the fleet total. Routes never include usernames,
query strings or request IDs. Unknown routes and methods use bounded fallback
labels. A controller `submitted` outcome means a Job was submitted, not that
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
label and timestamp. Initial failures show a short reason instead of zero usage
or a healthy status. Retry is available; repeated failures back off. Hidden tabs
pause polling. Missing monitoring configuration stops periodic retries and offers
setup guidance plus **Check again**. Network failures offer a sign-in link without
assuming every connection failure means an expired session.

Responses preserve existing fields and add a `problem` object with `code`, safe
`error`, `retryable`, `request_id` and `error_id`. The dashboard's **Copy reference**
action lets an operator find the matching backend event. Every observed response
also includes `X-Request-ID`, generated by the server rather than trusted from a
client header.

## Debugging logs

Collect stderr JSON from the controller and workspace containers using your
existing cluster log agent. Events contain timestamp, service, level and event.
Request completion includes the normalized route, status and duration. Failures
include correlation IDs where available and exception type plus stack locations.
Exception values and source lines are excluded because they can contain secrets.
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

| Alert | Default trigger | First action |
| --- | --- | --- |
| WorkspaceMemoryPressure | Working set exceeds 90% of a positive container limit for 5m | Inspect memory consumers and restart history; resize only after diagnosis |
| WorkspaceDiskPressure, warning | PVC usage above 85% for 15m | Inspect large files and plan cleanup or expansion |
| WorkspaceDiskPressure, critical | PVC usage above 95% for 5m | Free safe-to-remove data or expand the PVC promptly |
| WorkspaceHookDeadLetters | Dead letters remain for 5m | Check destination and credentials before retrying delivery |
| WorkspaceMetricsUnavailable | Running workspace has no healthy scrape for 10m | Check target selection, local scrape Secret and NetworkPolicy |
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
missing targets and sleeping workspaces.

Before production rollout, verify two controller replicas appear as separate
targets, tenant tokens cannot authorize task APIs, monitoring pods cannot reach
8080/8081, and a deliberate staging scrape failure triggers and resolves the
expected alert. Test the operator's notification route separately. Disable
PodMonitors and rules before disabling metrics or removing scrape Secrets.
Application JSON logging needs no additional writable volume.
