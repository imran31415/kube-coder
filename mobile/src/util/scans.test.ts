import { describe, expect, it } from 'vitest';
import {
  FINDING_FACTS,
  FINDING_SECTIONS,
  MODE_LABELS,
  emptyFindingsText,
  fieldText,
  isLiveScan,
  outcomeLabel,
  scanAgeLabel,
  severityRank,
  sortFindings,
  spendLabel,
  parseBudget,
  scanTitle,
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

  it('tells a stop the user asked for apart from one the scanner made', () => {
    expect(outcomeLabel(scan({ status: 'stopped', end_reason: 'user_stopped' }))).toBe(
      'Stopped by you — partly checked',
    );
    expect(outcomeLabel(scan({ status: 'stopped', end_reason: 'budget_reached' }))).toBe(
      'Hit its spending limit — partly checked',
    );
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

describe('parseBudget', () => {
  it('accepts a comma decimal separator rather than silently uncapping', () => {
    // The field is a decimal-pad, which types `,` on de/fr/es. `Number('5,50')`
    // is NaN, JSON.stringify writes NaN as null, and the server reads null as
    // "no cap" -- so the locale-correct way to type five-fifty started an
    // UNCAPPED deep scan under a label promising a limit.
    expect(parseBudget('5,50')).toBeCloseTo(5.5);
    expect(parseBudget('5.50')).toBeCloseTo(5.5);
  });

  it('reports unparseable text as invalid instead of as no limit', () => {
    for (const raw of ['$5', '5 usd', 'abc', '-1', '0', 'NaN']) {
      expect(parseBudget(raw)).toBe('invalid');
    }
  });

  it('treats only an empty field as a deliberate no-limit', () => {
    expect(parseBudget('')).toBeNull();
    expect(parseBudget('   ')).toBeNull();
  });
});

describe('scanTitle', () => {
  it('names the app, falling back to the port, never the address', () => {
    expect(scanTitle(scan())).toBe('shop');
    const unnamed = scan({ target: { port: 3000, name: '', url: 'http://172.17.0.1:3000' } });
    expect(scanTitle(unnamed)).toBe('Port 3000');
  });
});

describe('emptyFindingsText', () => {
  it('never calls an empty list an all-clear for a scan that ended early', () => {
    expect(emptyFindingsText(scan({ status: 'done' }))).toBe(
      'No problems were recorded for this scan.',
    );
    for (const status of ['stopped', 'interrupted', 'failed'] as const) {
      expect(emptyFindingsText(scan({ status }))).toContain(
        'None confirmed before the scan ended',
      );
    }
    expect(emptyFindingsText(scan({ status: 'running' }))).toContain('Nothing found yet');
  });

  it('points at the report only when there is one', () => {
    expect(emptyFindingsText(scan({ status: 'failed' }))).not.toContain('report');
    expect(emptyFindingsText({ status: 'stopped', report: { text: 'x' } })).toContain(
      'report is below',
    );
  });

  it('treats a finished-but-incomplete scan as not an all-clear', () => {
    const cut = scan({ status: 'done', coverage: { complete: false, caveats: ['cut short'] } });
    expect(emptyFindingsText(cut)).toContain('None confirmed before the scan ended');
    expect(outcomeLabel(cut)).toBe('Finished — but not all checked');
  });

  it('still says plainly when the scanner did cover the app', () => {
    const whole = scan({ status: 'done', coverage: { complete: true, caveats: [] } });
    expect(emptyFindingsText(whole)).toBe('No problems were recorded for this scan.');
    expect(outcomeLabel(whole)).toBe('Finished — nothing found');
  });
});
