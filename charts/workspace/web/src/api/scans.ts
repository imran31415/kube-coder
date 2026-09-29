import { apiGet, apiPost, apiDelete } from './client';

/**
 * Security scans (#726) — typed client for `/api/scans`.
 *
 * A finding is whatever the scanner wrote. Only the three fields the UI has to
 * reason about are typed; everything else rides in an index signature and is
 * rendered generically, so a scanner version that adds a field shows it
 * without a frontend change, and nothing here can quietly drop evidence.
 */

/** Ours, not the scanner's. `failed` is deliberately not `done`: an empty
 *  findings list from a scan that never ran must never read as a clean pass. */
export type ScanStatus = 'running' | 'done' | 'stopped' | 'failed' | 'interrupted';

export type Severity = 'critical' | 'high' | 'medium' | 'low';

export const SEVERITIES: Severity[] = ['critical', 'high', 'medium', 'low'];

export type ScanMode = 'quick' | 'standard' | 'deep';

/** How long each depth takes. Shown beside the choice, because the time cost
 *  IS part of the choice — the scanner's own default is the deepest one. */
export const MODE_LABELS: Record<ScanMode, string> = {
  quick: 'Quick — about 5 minutes',
  standard: 'Standard — 30 to 60 minutes',
  deep: 'Deep — 1 to 4 hours',
};

export interface Finding {
  id: string;
  severity?: string;
  title?: string;
  /** Everything else the scanner reported, rendered as-is. */
  [k: string]: unknown;
}

export interface ScanTarget {
  port: number;
  name: string;
  /** Bind address as the workspace sees it, e.g. "0.0.0.0" or "127.0.0.1". */
  addr: string;
  /** False when the app listens only on loopback, which the scanner cannot
   *  reach — it would connect to nothing and report no problems. */
  reachable: boolean;
  /** Why it is unreachable, in words fit to show. Empty when it is fine. */
  reason: string;
}

export interface ScanUsage {
  requests: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

export interface ScanCounts {
  critical: number;
  high: number;
  medium: number;
  low: number;
  /** Severities the scanner reported that we do not recognise. Never dropped. */
  other: number;
  total: number;
}

export interface ScanSummary {
  id: string;
  status: ScanStatus;
  backend: string;
  target: { port: number; name: string; url: string };
  mode: ScanMode;
  model: string;
  budget_usd: number | null;
  instruction: string;
  usage: ScanUsage;
  counts: ScanCounts;
  started_at: number;
  ended_at: number | null;
  error: string | null;
}

export interface ScanDetail extends ScanSummary {
  findings: Finding[];
  /** What the USER decided about a finding, keyed by finding id. Kept apart
   *  from the findings so the scanner's own record is never edited. */
  dispositions: Record<string, 'dismissed'>;
  /** One honest sentence about how the scan ended — never a bare "0 found". */
  summary: string;
}

export interface InstallState {
  state: 'absent' | 'installing' | 'ready' | 'failed';
  version: string;
  error: string;
}

export interface SubscriptionView {
  /** Whether a subscription sign-in has been completed. Never the token. */
  signed_in: boolean;
}

export interface ConnectionView {
  model: string;
  api_base: string;
  /** Whether a key is stored. The key itself is never sent to the browser. */
  has_key: boolean;
  uses_subscription: boolean;
  subscription: SubscriptionView;
  configured: boolean;
  install: InstallState;
  backend: { ok: boolean; reason: string; detail: string };
}

export interface CreateScanBody {
  port: number;
  mode: ScanMode;
  /** Omit to use the connected model. */
  model?: string;
  /** Null or omitted means no cap, exactly as the scanner has it. */
  budget_usd?: number | null;
  instruction?: string;
}

export const listScans = () => apiGet<{ scans: ScanSummary[] }>('/api/scans');

export const getScan = (id: string) => apiGet<ScanDetail>(`/api/scans/${id}`);

export const listScanTargets = () =>
  apiGet<{ targets: ScanTarget[] }>('/api/scans/targets');

export const createScan = (body: CreateScanBody) =>
  apiPost<{ scan_id: string; status: ScanStatus; target: ScanSummary['target'] }>(
    '/api/scans',
    body,
  );

export const stopScan = (id: string) =>
  apiPost<{ ok: true; status: ScanStatus }>(`/api/scans/${id}/stop`, {});

export const deleteScan = (id: string) =>
  apiDelete<{ ok: true; removed: boolean }>(`/api/scans/${id}`);

export const setFindingDisposition = (
  scanId: string,
  findingId: string,
  disposition: 'open' | 'dismissed',
) =>
  apiPost<{ ok: true; dispositions: Record<string, 'dismissed'> }>(
    `/api/scans/${scanId}/findings/${encodeURIComponent(findingId)}/disposition`,
    { disposition },
  );

export const getScanConnection = () =>
  apiGet<ConnectionView>('/api/scans/connection');

export const saveScanConnection = (body: {
  model?: string;
  api_key?: string;
  api_base?: string;
}) => apiPost<ConnectionView>('/api/scans/connection', body);

export const clearScanConnection = () =>
  apiDelete<ConnectionView>('/api/scans/connection');

/**
 * Signing in to a subscription is four calls, not one: the user completes it
 * in their OWN browser, so the workspace hands over a link, waits for them to
 * paste back where they landed, and polls until the scanner confirms it.
 */
export const startScanSignIn = () =>
  apiPost<{ url: string; in_progress: boolean }>(
    '/api/scans/connection/signin/start',
    {},
  );

/** Forward what the user pasted. Single-use — nothing keeps a copy. */
export const submitScanSignIn = (redirect: string) =>
  apiPost<{ ok: true }>('/api/scans/connection/signin/submit', { redirect });

export const pollScanSignIn = () =>
  apiPost<{
    signed_in: boolean;
    in_progress: boolean;
    error?: string;
    connection?: ConnectionView;
  }>('/api/scans/connection/signin/poll', {});

export const cancelScanSignIn = () =>
  apiPost<{ ok: true }>('/api/scans/connection/signin/cancel', {});

export const signOutScanSubscription = () =>
  apiPost<{ ok: boolean; connection: ConnectionView }>(
    '/api/scans/connection/signout',
    {},
  );

export const testScanConnection = () =>
  apiPost<{ ok: boolean; detail: string }>('/api/scans/connection/test', {});

// ---- presentation helpers (pure; unit-tested) -----------------------------

/** A scan that is still working. Drives polling and the Stop button. */
export const isLiveScan = (s: { status: ScanStatus }): boolean =>
  s.status === 'running';

/** Severity order for display: worst first, unknown last but never hidden. */
export function severityRank(finding: Finding): number {
  const sev = (finding.severity || '').toLowerCase() as Severity;
  const idx = SEVERITIES.indexOf(sev);
  return idx < 0 ? SEVERITIES.length : idx;
}

export function sortFindings(findings: Finding[]): Finding[] {
  return [...findings].sort(
    (a, b) => severityRank(a) - severityRank(b) || a.id.localeCompare(b.id),
  );
}

/**
 * What a scan's outcome should say in a list row.
 *
 * Never a bare count. Every way a scan can fail to reach the app produces an
 * empty findings list, identical to a genuine clean result, so a row that says
 * only "0" over a scan that never connected is actively misleading.
 */
export function outcomeLabel(scan: ScanSummary): string {
  if (scan.status === 'running') return 'Scanning…';
  if (scan.status === 'failed') return 'Could not run — nothing was checked';
  if (scan.status === 'interrupted') return 'Interrupted — partly checked';
  if (scan.status === 'stopped') return 'Stopped early — partly checked';
  const n = scan.counts?.total ?? 0;
  return n ? `${n} to look at` : 'Finished — nothing found';
}

/** Money already spent, as the scanner reports it. */
export function spendLabel(scan: { usage?: ScanUsage; budget_usd: number | null }): string {
  const spent = scan.usage?.cost_usd ?? 0;
  const cap = scan.budget_usd;
  const spentText = `$${spent.toFixed(4)}`;
  return cap ? `${spentText} of $${cap.toFixed(2)}` : spentText;
}

/** Short relative age, e.g. "4m ago". */
export function agoLabel(ts: number, now: number = Date.now()): string {
  const secs = Math.max(0, Math.floor(now / 1000 - ts));
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** How long a scan ran (or has been running). */
export function durationLabel(scan: ScanSummary, now: number = Date.now()): string {
  const end = scan.ended_at ?? now / 1000;
  const secs = Math.max(0, Math.floor(end - scan.started_at));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

/**
 * The fields of a finding that get their own section, in reading order.
 *
 * Anything the scanner reports that is NOT listed here is still rendered,
 * generically, after these — so a new field appears rather than vanishing.
 */
export const FINDING_SECTIONS: { key: string; label: string; code?: boolean }[] = [
  { key: 'description', label: 'What it is' },
  { key: 'impact', label: 'Why it matters' },
  { key: 'evidence', label: 'Evidence', code: true },
  { key: 'poc_description', label: 'How to reproduce it' },
  { key: 'poc_script_code', label: 'Reproduction script', code: true },
  { key: 'technical_analysis', label: 'Technical detail' },
  { key: 'remediation_steps', label: 'How to fix it' },
  { key: 'fix_verification', label: 'How to check the fix' },
  { key: 'counterevidence', label: 'Evidence against' },
  { key: 'confidence_rationale', label: 'Why this confidence' },
  { key: 'severity_change_conditions', label: 'What would change the severity' },
  { key: 'assumptions', label: 'Assumptions' },
];

/** Short scalar facts shown as chips on a finding. */
export const FINDING_FACTS: { key: string; label: string }[] = [
  { key: 'endpoint', label: 'Endpoint' },
  { key: 'method', label: 'Method' },
  { key: 'cvss', label: 'CVSS' },
  { key: 'cve', label: 'CVE' },
  { key: 'cwe', label: 'CWE' },
  { key: 'confidence', label: 'Confidence' },
  { key: 'fix_effort', label: 'Fix effort' },
];

/** Keys already rendered elsewhere, so the generic tail does not repeat them. */
const RENDERED = new Set<string>([
  'id', 'title', 'severity', 'timestamp', 'updated_at', 'target',
  'agent_id', 'agent_name', 'update_history', 'http_exchange_ids',
  ...FINDING_SECTIONS.map((s) => s.key),
  ...FINDING_FACTS.map((f) => f.key),
]);

/** Anything the scanner reported that this UI has no specific place for. */
export function extraFields(finding: Finding): { key: string; value: string }[] {
  return Object.entries(finding)
    .filter(([k, v]) => !RENDERED.has(k) && v !== null && v !== '' && v !== undefined)
    .map(([key, value]) => ({
      key,
      value: typeof value === 'string' ? value : JSON.stringify(value, null, 2),
    }));
}
