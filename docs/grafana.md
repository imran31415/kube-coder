# Grafana dashboards

kube-coder ships two Grafana dashboards for the metrics described in
[platform-observability.md](platform-observability.md) and
[prometheus-metrics.md](prometheus-metrics.md):

| Dashboard | Shows |
| --- | --- |
| **kube-coder / Fleet** | Scrape health of the controller and every workspace, HTTP traffic, errors and latency for both services, controller operations (scale, update, resources, provisioning), tasks by status, agent tokens and how much of the fleet is measured, and per-workspace CPU, memory and volume usage |
| **kube-coder / Workspace** | One workspace, picked from a dropdown: tasks, agent runs and tokens by class, model and scope, boards (approval rate, open reviews, decisions, dispositions), completion hooks, memory embeddings, upload storage against its quota, HTTP, and per-container resources |

They live in `charts/workspace-controller/dashboards/` as plain Grafana JSON,
and the workspace-controller chart turns each one into a ConfigMap that a
Grafana dashboard sidecar loads. kube-coder does not run its own Grafana: you
point the dashboards at the one you have, or install the upstream Grafana chart
with one command.

## Prerequisites

Metrics collection set up as in
[platform-observability.md](platform-observability.md#enable-collection): the
controller's metrics on, and `metrics.scrapeSecretName` plus
`metrics.podMonitor.enabled` on each workspace you want to see. A workspace that
isn't scraped is missing from the Workspace dropdown and from the Fleet
dashboard's per-workspace panels. The resource panels also use the kubelet and
cAdvisor metrics that kube-prometheus-stack collects by default, and the
memory-limit lines use kube-state-metrics.

## Option A: you already run Grafana (kube-prometheus-stack)

kube-prometheus-stack's Grafana watches for ConfigMaps labelled
`grafana_dashboard: "1"`. Turn the dashboards on in your workspace-controller
values and upgrade:

```yaml
grafanaDashboards:
  enabled: true
```

```bash
make ship-controller-config      # or: helm upgrade workspace-controller ...
```

Where the ConfigMaps must live depends on the sidecar's `searchNamespace`:

- **`ALL`** (kube-prometheus-stack's default): any namespace works, so leave
  `grafanaDashboards.namespace` empty and they go to the controller's
  namespace.
- **Unset (the sidecar's own namespace only):** set
  `grafanaDashboards.namespace` to Grafana's namespace.

Check what your sidecar watches with:

```bash
kubectl get deploy -A -l app.kubernetes.io/name=grafana -o yaml | grep -A1 -E 'name: (LABEL|LABEL_VALUE|NAMESPACE)$'
```

If your sidecar uses a different label, set `grafanaDashboards.label` and
`grafanaDashboards.labelValue` to match. The dashboards go to the
**kube-coder** folder when the sidecar sets `folderAnnotation: grafana_folder`,
and to **General** otherwise.

## Option B: no Grafana yet

`make grafana` installs the upstream
[grafana chart](https://github.com/grafana/helm-charts), pinned in the Makefile
and kept current by Renovate. The values it applies are in
`charts/workspace-controller/grafana/standalone-values.yaml`:

- a Prometheus datasource, set as the default;
- a dashboard sidecar that watches **only its own namespace**, using a Role
  rather than a ClusterRole;
- no persistence (dashboards and the datasource are re-provisioned on every
  start), no anonymous access, no sign-up, and no Ingress.

```bash
# 1. Grafana, in the controller's namespace (default: coder).
make grafana GRAFANA_PROMETHEUS_URL=http://<prometheus-service>.<ns>.svc:9090

# 2. The dashboards, from the controller chart (same namespace).
#    In your controller values: grafanaDashboards.enabled: true
make ship-controller-config

# 3. Open it.
make grafana-password            # admin password
make grafana-forward             # http://localhost:3000, user admin
```

`GRAFANA_PROMETHEUS_URL` defaults to the kube-prometheus-stack service name the
controller's `metrics.prometheusUrl` also defaults to. To install Grafana in a
different namespace, set `GRAFANA_NAMESPACE` and give the controller the same
value as `grafanaDashboards.namespace`. `make grafana-uninstall` removes
Grafana and leaves the dashboard ConfigMaps with the controller chart.

To reach Grafana without port-forwarding, add an `ingress:` block to a copy of
the values file, behind your SSO (oauth2-proxy, as the workspaces use). Don't
expose it with only the admin password.

## Reading the dashboards

- **Pick the datasource** at the top if you have more than one Prometheus. Every
  panel queries through that variable; nothing names a datasource directly.
- **"Workspaces reporting" vs "Workspace scrapes failing".** A failing scrape
  means Prometheus found the workspace but couldn't read it: check its
  `kc-metrics-scrape` Secret and the NetworkPolicy
  ([Verify](platform-observability.md#3-verify)). A workspace that's absent from
  both isn't selected at all, so check `metrics.podMonitor.enabled` and the
  monitor labels.
- **Token panels are ledger gauges, not counters.** "Tokens added in range" is
  the growth over the selected time range, clamped at zero. If a workspace was
  first scraped inside the range, its whole total counts as growth, the same
  way the controller's spend view counts it. Check "Agent runs by measurement
  coverage" before trusting a token total: only Claude Code reports usage.
- **Controller operations stay empty** until someone scales, updates or
  provisions a workspace after the controller starts. The panels say so.
- **Collector sections.** A DOWN section means that part of the workspace's
  metrics is absent for the period, not zero.

## Changing a dashboard

1. Edit it in Grafana. Provisioned dashboards can't be saved in place, so use
   **Save as** to make a copy, or edit and export.
2. **Export → Export as JSON**, with **"Export for sharing externally" off**.
   Leaving it on replaces the datasource variable with a placeholder input the
   sidecar can't fill.
3. Replace the file in `charts/workspace-controller/dashboards/`, keeping its
   `uid` equal to the file name.
4. Run `make controller-python-tests`. `tests/dashboards_test.py` fails if a
   panel queries a `kubecoder_*` metric no kube-coder source defines, hard-codes
   a datasource, or carries a workspace name or URL from your cluster.

To add a dashboard, drop a new `<uid>.json` into the same directory. The chart
picks up every `*.json` in it.
