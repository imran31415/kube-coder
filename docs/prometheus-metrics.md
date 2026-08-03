# Prometheus metrics

The workspace exposes its own metrics in Prometheus text exposition format at
**`GET /metrics/prometheus`** (issue #105).

This is a *different endpoint* from `/metrics`, which returns JSON and is what
the dashboard's Metrics page reads. Both are kept: the JSON one is a UI feed
with nested objects and top-N lists that have no sensible flat representation;
this one is a scrape target. They are separate paths rather than one URL
negotiating on `Accept`, because a shared URL serving two representations needs
a correct `Vary: Accept` to survive the ingress and oauth2-proxy in front of the
pod, and one client library changing its default `Accept` header would blank the
dashboard with no error anywhere. `metrics_path` is a one-line field in any
scrape config, so the separate path costs nothing.

## Scraping it

The endpoint uses the same auth gate as the JSON `/metrics`: the workspace's
Claude Task API token, as a Bearer credential.

```bash
TOKEN=$(cat /home/dev/.claude-tasks/.api-token)
curl -H "Authorization: Bearer $TOKEN" localhost:6080/metrics/prometheus
```

With the Prometheus Operator:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: kube-coder-workspace
spec:
  selector:
    matchLabels: { app: workspace }        # your workspace Service labels
  endpoints:
    - port: http                            # the 6080 port
      path: /metrics/prometheus
      interval: 30s
      authorization:
        credentials:
          # one Secret per workspace — the token is per-workspace state on the
          # PVC, not a cluster-wide credential
          name: <workspace>-claude-api-token
          key: token
```

## What is exposed

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `kubecoder_agent_tokens` | gauge | `scope`, `model`, `class` | Agent tokens in the ledgers currently on disk, split by the four priceable classes (#574). |
| `kubecoder_agent_tokens_unclassified` | gauge | `scope` | Pre-#574 tokens whose input classes were summed before being recorded and cannot be split apart again. |
| `kubecoder_agent_runs` | gauge | `scope`, `coverage` | Agent runs by how measurable their spend is. |
| `kubecoder_tasks` | gauge | `status` | Tasks on this workspace by status. |
| `kubecoder_hook_deliveries_total` | **counter** | `outcome` | Completion-hook deliveries that reached a terminal outcome since this process started. |
| `kubecoder_hook_dead_letters` | gauge | — | Tasks whose completion hook exhausted its retries. |
| `kubecoder_memory_embeddings_pending` | gauge | — | Memories written but not yet vectorised. |
| `kubecoder_memory_embeddings_worker_up` | gauge | — | 1 when the embedding worker thread is alive. |
| `kubecoder_metrics_collector_up` | gauge | `section` | 1 when that section of the exposition was collected successfully. |

Label vocabularies:

* `scope` — `threads` (Hypervisor chat) or `builds` (background tasks). There is
  deliberately **no** `all` scope: `sum()` over the two gives the total, and an
  `all` series alongside them would double-count anyone who summed.
* `class` — `input`, `cache_read`, `cache_write`, `output`. These bill at very
  different rates, which is why they are kept apart.
* `coverage` — `measured`, `not_instrumented`, `no_session_id`. See below.
* `status` — `running`, `waiting-for-input`, `completed`, `error`, `killed`,
  `unknown`. Anything else folds into `unknown`.
* `model` — the model id from the ledger, capped at 20 values per scope with the
  remainder folded into `model="other"`, plus `unknown` (a ledger entry with no
  model) and `unattributed` (see below).

## Reading the token metrics correctly

**Summing over `model` gives exactly the ledger's own class total.** Where the
per-model breakdown does not account for the whole figure, the difference is
emitted as `model="unattributed"` rather than dropped, so a partial breakdown
never under-reports. That series is emitted even when it is zero.

**A zero is not always a measurement.** Only Claude Code reports token usage;
Codex, Ante, OpenCode, LibreFang and the open-source harness report nothing at
all, so their spend shows as 0. `kubecoder_agent_runs` is the marker that tells
them apart — a fleet total is only as complete as that metric says it is:

```promql
# spend, and how much of the fleet it actually covers
sum by (model, class) (kubecoder_agent_tokens)
sum by (coverage) (kubecoder_agent_runs)
```

**`kubecoder_agent_tokens_unclassified` is counted but not priceable.** Its
tokens are real, but their input-class mix was lost before it was recorded;
pricing them as fresh input would overstate a cache read by roughly 10x. It is a
separate metric precisely so that summing `kubecoder_agent_tokens` can never
include them by accident.

## Why almost everything is a gauge

Prometheus counters may only increase; a decrease is read as a process restart,
and `rate()` then credits the whole post-decrease value as fresh increase.

Every figure above except `kubecoder_hook_deliveries_total` is recomputed from
files on disk on each scrape — task metadata, thread metadata, the memory
database. Deleting a task removes its ledger, and the total falls. Those are
gauges, and cumulative growth over a window is `x - (x offset 7d)`, not
`increase()`.

`kubecoder_hook_deliveries_total` is incremented in memory as each delivery
reaches its terminal outcome, so it is monotonic for the life of the process.
It resets to zero on restart, which is the one decrease Prometheus models
correctly. `kubecoder_hook_dead_letters` is the disk-backed companion that
survives a restart — alert on the gauge, rate the counter.

## Cardinality

Every label is drawn from a fixed vocabulary except `model`, which is capped.
Nothing is labelled by task id, thread id, session id, workdir, project or URL:
each distinct value of those becomes a permanent time series, and that is the
usual way a Prometheus is taken down. A workspace with any amount of history
produces well under 100 series here, and that number does not grow with the
number of tasks.

## Scrape cost

One `os.listdir` plus one small `json.load` per task, one metadata read per
Hypervisor thread, and one indexed `COUNT(*)` on the embedding queue — the same
work the JSON `/metrics` already does on every dashboard poll, with the task
walk shared between the token and task sections rather than run twice.

Deliberately **not** exposed: CPU utilisation (sampling it means differencing
`/proc/stat` across a 500 ms sleep, which is not acceptable inside a scrape),
RAM and disk (kubelet and cAdvisor already export both, per container and per
PVC), and the memory store's recall counts and skill invocation counts (their
natural label is a memory key or skill name, which is unbounded).

If a section's source is unavailable or errors, its metrics are **absent** and
`kubecoder_metrics_collector_up{section="…"}` is 0 — so a broken collector never
reads as a genuine zero.
