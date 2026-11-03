/**
 * Pure scan helpers (#726) — kept React-Native-free so the node-side vitest
 * suite can exercise them, mirroring `util/feed.ts`.
 *
 * These carry the one presentational rule that actually matters: an empty
 * findings list is produced by a clean scan AND by every way a scan can fail
 * to reach the app, so nothing here ever renders a bare count.
 */
import type { Finding, ScanSummary, ScanMode, ScanUsage } from '../api/types';
import { SCAN_SEVERITIES } from '../api/types';

/** How long each depth takes. Shown beside the choice, because the scanner's
 *  own default is its deepest mode and the time cost IS part of the choice. */
export const MODE_LABELS: Record<ScanMode, string> = {
  quick: 'Quick · ~5 min',
  standard: 'Standard · ~45 min',
  deep: 'Deep · 1–4 hrs',
};

/** A scan still doing work — what drives polling and the Stop button. */
export function isLiveScan(scan: { status: string }): boolean {
  return scan.status === 'running';
}

/**
 * What the scan is OF: the app's name, else its port. Never the address the
 * scanner reached it on — an internal container address the user cannot open.
 */
export function scanTitle(scan: { target: { name: string; port: number } }): string {
  return scan.target.name || `Port ${scan.target.port}`;
}

/** Sort key: worst first, unknown severity last but never hidden. */
export function severityRank(finding: Finding): number {
  const sev = (finding.severity || '').toLowerCase();
  const idx = (SCAN_SEVERITIES as readonly string[]).indexOf(sev);
  return idx < 0 ? SCAN_SEVERITIES.length : idx;
}

export function sortFindings(findings: Finding[]): Finding[] {
  return [...findings].sort(
    (a, b) => severityRank(a) - severityRank(b) || a.id.localeCompare(b.id),
  );
}

/**
 * What a scan's outcome says in a list row.
 *
 * Never a bare number. "0 findings" over a scan whose credentials were
 * rejected, or that never reached the app, reads as an all-clear — which is
 * the most dangerous thing a security surface can say.
 */
export function outcomeLabel(scan: ScanSummary): string {
  if (scan.status === 'running') return 'Scanning…';
  if (scan.end_reason === 'user_stopped') return 'Stopped by you — partly checked';
  if (scan.end_reason === 'budget_reached') return 'Hit its spending limit — partly checked';
  if (scan.status === 'failed') return 'Could not run — nothing was checked';
  if (scan.status === 'interrupted') return 'Interrupted — partly checked';
  if (scan.status === 'stopped') return 'Stopped early — partly checked';
  const n = scan.counts?.total ?? 0;
  if (n) return `${n} to look at`;
  // Only an all-clear when the scanner says it got through the app: it can
  // finish, write its report, and separately record that it ran out of room.
  return scan.coverage?.complete === false
    ? 'Finished — but not all checked'
    : 'Finished — nothing found';
}

/**
 * What an empty problems list says. Mirrors the dashboard's helper of the
 * same name: a problem is listed only once the scanner has proven it, so a
 * scan that ended early usually lists none, and "no problems were recorded"
 * there reads as an all-clear for a scan that never got that far.
 */
export function emptyFindingsText(scan: {
  status: string;
  coverage?: { complete: boolean };
  report?: unknown;
}): string {
  if (scan.status === 'running') {
    return 'Nothing found yet. A problem shows up here the moment the scanner proves it is real.';
  }
  if (scan.status === 'done' && scan.coverage?.complete !== false) {
    return 'No problems were recorded for this scan.';
  }
  // Only when there is a report: a scan that failed at start never writes one.
  return (
    'None confirmed before the scan ended. A problem is listed only once the ' +
    'scanner has proven it, so a scan that ends early often shows none.' +
    (scan.report ? ' The scanner’s own report is below.' : '')
  );
}

/** Money spent, against the cap when there is one. */
export function spendLabel(scan: {
  usage?: ScanUsage;
  budget_usd: number | null;
}): string {
  const spent = scan.usage?.cost_usd ?? 0;
  const text = `$${spent.toFixed(4)}`;
  return scan.budget_usd ? `${text} of $${scan.budget_usd.toFixed(2)}` : text;
}

/** Short relative age for a unix-seconds timestamp. */
export function scanAgeLabel(ts: number, now: number = Date.now()): string {
  const secs = Math.max(0, Math.floor(now / 1000 - ts));
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** Fields of a finding that get their own heading, in reading order. */
export const FINDING_SECTIONS: { key: string; label: string; code?: boolean }[] = [
  { key: 'description', label: 'What it is' },
  { key: 'impact', label: 'Why it matters' },
  { key: 'evidence', label: 'Evidence', code: true },
  { key: 'poc_description', label: 'How to reproduce it' },
  { key: 'poc_script_code', label: 'Reproduction', code: true },
  { key: 'technical_analysis', label: 'Technical detail' },
  { key: 'remediation_steps', label: 'How to fix it' },
  { key: 'fix_verification', label: 'How to check the fix' },
];

/** Short scalar facts shown as chips. */
export const FINDING_FACTS: { key: string; label: string }[] = [
  { key: 'endpoint', label: 'Endpoint' },
  { key: 'method', label: 'Method' },
  { key: 'cvss', label: 'CVSS' },
  { key: 'cve', label: 'CVE' },
  { key: 'cwe', label: 'CWE' },
  { key: 'confidence', label: 'Confidence' },
];

/** Render any scanner-reported value as text, without losing it. */
export function fieldText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

/**
 * The typed spending limit as the API wants it, or `'invalid'`.
 *
 * `Number(budget)` was used directly at the call site, and anything it cannot
 * parse becomes NaN -- which `JSON.stringify` writes as `null`, which the
 * server reads as "no cap". The field is a `decimal-pad`, which renders a
 * comma separator on de/fr/es, so a user typing `5,50` -- the correct way to
 * write it there -- silently started an *uncapped* deep scan under a label
 * promising a limit. The server's own validator cannot catch it either,
 * because the bad value arrives as `null` rather than as a bad number.
 *
 * A comma is accepted rather than rejected, for that same reason.
 */
export function parseBudget(raw: string): number | null | 'invalid' {
  const text = (raw ?? '').trim();
  if (text === '') return null;
  const value = Number(text.replace(',', '.'));
  return Number.isFinite(value) && value > 0 ? value : 'invalid';
}
