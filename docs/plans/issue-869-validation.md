# Issue #869 implementation and validation

Implemented on `feat/issue-869-observability`. No live deployment was performed.

## Delivered

- Adapted the existing scrape-token and PodMonitor work to the current charts,
  preserving cron/keeper ingress rules and isolating controller metrics from
  admin and self-serve listeners.
- Added stdlib HTTP and controller-operation counters/histograms with bounded
  labels, separate stream/proxy lifetime accounting, and shared helper parity.
- Added JSON stderr logging, safe correlated failures and request deadlines
  propagated through parallel namespace reads.
- Reused existing dashboard panels with shared polling, cancellation, stale-data
  labels, short safe errors, retry/check-again, and copyable error references.
- Added optional alert rules, Helm/promtool regression coverage and CI checks.
- Added the operator setup and troubleshooting guide at
  [platform-observability.md](../platform-observability.md).

No runtime dependencies or lockfile changes were introduced. Canonical helpers
live in `charts/workspace`; `scripts/sync-observability.py` generates the copies
embedded in the controller ConfigMap. Existing unrelated local files were kept.

## Checks

| Check | Result |
| --- | --- |
| Controller Python suite | 137 passed on 2026-11-13 |
| Workspace observability, exposition and metrics suites | 108 passed on 2026-11-13 |
| Workspace safe HTTP, routing, auth matrix and hook delivery suites | 85 passed on 2026-11-13 |
| Controller frontend tests | 47 passed on 2026-11-13 |
| TypeScript type check | Passed on 2026-11-13 |
| Workspace Helm unit tests | 321 passed |
| Controller Helm unit tests | 87 passed |
| Rendered Prometheus rules | All six rules validated with promtool 3.5.0; all three scenario groups passed |
| Production frontend build | Passed; single HTML artifact approximately 82 kB |
| Helper byte parity and whitespace check | Passed |

The promtool scenarios cover firing delays and recovery, duplicate scrape
series, zero memory limits, disk warning/critical windows, absent scrape targets,
sleeping workspaces, dead letters and collector failures.

The repository-wide workspace Python run was interrupted when WSL became
unresponsive; it has no complete result and is not counted as passing. The
focused suites above were rerun successfully after WSL became available.

## Remaining environment verification

No live cluster was changed. Operator-owned collection and notification delivery,
actual CNI enforcement, two-replica scraping, rollout and token rotation need a
staging check using the guide. Browser visual checks were unavailable because the
browser skill's required JavaScript execution tool was not exposed. Component
tests verify retry, stale readings, cancellation and late-response isolation,
but do not establish visual quality at mobile and desktop sizes.
