import { apiGet } from './client';

// Mirrors controller.py:fleet_spend (#581). Every figure is aggregated out of
// the same Prometheus the capacity page queries, from the per-workspace
// exposition at /metrics/prometheus (#105/#603) — the controller never talks to
// a workspace directly, so a workspace that is currently asleep still counts.

/** The four priceable token classes (#574). They bill at very different rates,
 *  which is why the API never collapses them into one number. */
export type TokenClass = 'input' | 'cache_read' | 'cache_write' | 'output';

export interface TokenBlock {
  input: number;
  cache_read: number;
  cache_write: number;
  output: number;
  /** Sum of the four classes above. */
  total: number;
  /** Real spend whose input-class mix was lost before it was recorded (pre-#574
   *  ledgers). Deliberately NOT inside `total`: pricing it as fresh input
   *  overstates a cache read by roughly 10x. */
  unclassified: number;
}

/** `window` is growth over the requested range; `current` is everything the
 *  ledgers on disk still hold. They differ whenever tasks have been deleted. */
export interface SpendPair {
  window: TokenBlock;
  current: TokenBlock;
}

export interface WorkspaceSpend extends SpendPair {
  user: string;
  namespace: string;
}

export interface ModelSpend extends SpendPair {
  /** A model id, `unknown`, `unattributed` (spend the ledger could not pin on
   *  a model) or `other` (the folded tail beyond the cardinality cap). */
  model: string;
}

/** How measurable the fleet's runs were. Only `measured` runs can report spend
 *  at all, so a total is only as complete as this says it is. */
export interface CoverageCounts {
  measured: number;
  not_instrumented: number;
  no_session_id: number;
}

export interface SpendResponse {
  generatedAt: number;
  windowSeconds: number;
  fleet: SpendPair;
  byWorkspace: WorkspaceSpend[];
  byModel: ModelSpend[];
  coverage: { window: CoverageCounts; current: CoverageCounts };
  workspacesReporting: number;
  /** Set when Prometheus is unset or unreachable — the numbers are absent, not
   *  zero. */
  metricsError: string | null;
  /** Set when Prometheus answered but no workspace exports the spend series:
   *  a scrape gap, which also looks like a page of zeroes. */
  scrapeHint: string | null;
}

export const getSpend = (rangeSeconds = 21600) =>
  apiGet<SpendResponse>('/api/spend', { range: rangeSeconds });
