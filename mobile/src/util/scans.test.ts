import { describe, expect, it } from 'vitest';
import {
  FINDING_FACTS,
  FINDING_SECTIONS,
  MODE_LABELS,
  fieldText,
  isLiveScan,
  outcomeLabel,
  scanAgeLabel,
  severityRank,
  sortFindings,
  spendLabel,
} from './scans';
import type { Finding, ScanSummary } from '../api/types';

function scan(over: Partial<ScanSummary> = {}): ScanSummary {
  return {
    id: 'scn_000000000001',
    status: 'done',
    backend: 'local-strix',
    target: { port: 3000, name: 'shop', url: 'http://10.0.0.1:3000' },
    mode: 'quick',
    model: 'prov/model',
    budget_usd: 5,
    instruction: '',
    usage: {
      requests: 3,
      input_tokens: 100,
      output_tokens: 10,
      total_tokens: 110,
      cost_usd: 0.231,
    },
    counts: { critical: 0, high: 0, medium: 0, low: 0, other: 0, total: 0 },
    started_at: 1_759_046_400,
    ended_at: 1_759_046_700,
    error: null,
    ...over,
  };
}

describe('scan helpers', () => {
  it('treats only a running scan as live', () => {
    expect(isLiveScan(scan({ status: 'running' }))).toBe(true);
    for (const status of ['done', 'stopped', 'failed', 'interrupted'] as const) {
      expect(isLiveScan(scan({ status }))).toBe(false);
    }
  });

  it('shows the time cost beside every depth', () => {
    // The scanner's own default is its deepest mode, so how long each takes
    // has to be part of the choice rather than a surprise afterwards.
    for (const label of Object.values(MODE_LABELS)) {
      expect(label).toMatch(/min|hr/);
    }
  });

  it('never reports a bare zero for a scan that could not run', () => {
    expect(outcomeLabel(scan({ status: 'failed' }))).toMatch(/nothing was checked/i);
    expect(outcomeLabel(scan({ status: 'interrupted' }))).toMatch(/partly/i);
    expect(outcomeLabel(scan({ status: 'stopped' }))).toMatch(/partly/i);
  });

  it('says a clean scan finished rather than just "0"', () => {
    const label = outcomeLabel(scan());
    expect(label).toMatch(/finished/i);
    expect(label).not.toBe('0');
  });

  it('counts findings when there are some', () => {
    expect(
      outcomeLabel(
        scan({ counts: { critical: 1, high: 2, medium: 0, low: 0, other: 0, total: 3 } }),
      ),
    ).toContain('3');
  });

  it('orders findings worst first and keeps an unknown severity visible', () => {
    const findings: Finding[] = [
      { id: 'c', severity: 'low' },
      { id: 'a', severity: 'not-a-severity' },
      { id: 'b', severity: 'critical' },
    ];
    expect(sortFindings(findings).map((f) => f.id)).toEqual(['b', 'c', 'a']);
    expect(severityRank({ id: 'x' })).toBeGreaterThan(
      severityRank({ id: 'y', severity: 'low' }),
    );
  });

  it('shows spend against a cap when there is one', () => {
    expect(spendLabel(scan())).toBe('$0.2310 of $5.00');
    expect(spendLabel(scan({ budget_usd: null }))).toBe('$0.2310');
  });

  it('formats ages', () => {
    const now = 1_759_046_400_000;
    expect(scanAgeLabel(1_759_046_400, now)).toBe('just now');
    expect(scanAgeLabel(1_759_046_400 - 300, now)).toBe('5m ago');
    expect(scanAgeLabel(1_759_046_400 - 7200, now)).toBe('2h ago');
  });

  it('renders any reported value as text without losing it', () => {
    expect(fieldText('plain')).toBe('plain');
    expect(fieldText(9.1)).toBe('9.1');
    expect(fieldText({ a: 1 })).toContain('"a"');
    expect(fieldText(null)).toBe('');
  });

  it('gives every section and fact a human label', () => {
    for (const entry of [...FINDING_SECTIONS, ...FINDING_FACTS]) {
      expect(entry.label).toBeTruthy();
      expect(entry.label).not.toBe(entry.key);
    }
  });
});
