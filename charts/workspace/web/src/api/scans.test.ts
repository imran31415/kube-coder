import { describe, it, expect, afterEach, vi } from 'vitest';
import {
  createScan,
  deleteScan,
  getScan,
  listScans,
  listScanTargets,
  saveScanConnection,
  setFindingDisposition,
  stopScan,
  testScanConnection,
  agoLabel,
  budgetFraction,
  durationLabel,
  emptyFindingsText,
  extraFields,
  modePhrase,
  moneyLabel,
  scanTitle,
  startedLabel,
  isLiveScan,
  outcomeLabel,
  outcomeView,
  reportSavedText,
  retryBody,
  STATUS_LABELS,
  severityRank,
  sortFindings,
  spendLabel,
  MODE_LABELS,
  type Finding,
  type ScanSummary,
} from './scans';

const realFetch = globalThis.fetch;

function respond(status: number, body: unknown) {
  const calls: { url: string; method: string; body: string | null }[] = [];
  globalThis.fetch = vi.fn(async (url: unknown, init?: RequestInit) => {
    calls.push({
      url: String(url),
      method: init?.method ?? 'GET',
      body: (init?.body as string) ?? null,
    });
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => 'application/json' },
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

function scan(over: Partial<ScanSummary> = {}): ScanSummary {
  return {
    id: 'scn_abc123def456',
    status: 'done',
    backend: 'local-strix',
    target: { port: 3000, name: 'shop', url: 'http://10.0.0.1:3000' },
    mode: 'quick',
    model: 'prov/model',
    budget_usd: 5,
    instruction: '',
    usage: { requests: 3, input_tokens: 100, output_tokens: 10, total_tokens: 110, cost_usd: 0.231 },
    counts: { critical: 0, high: 0, medium: 0, low: 0, other: 0, total: 0 },
    started_at: 1_759_046_400,
    ended_at: 1_759_046_700,
    error: null,
    ...over,
  };
}

describe('scans api client', () => {
  afterEach(() => {
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('lists scans', async () => {
    const calls = respond(200, { scans: [] });
    await listScans();
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toContain('/api/scans');
  });

  it('fetches one scan and the target list', async () => {
    let calls = respond(200, {});
    await getScan('scn_abc123def456');
    expect(calls[0].url).toContain('/api/scans/scn_abc123def456');

    calls = respond(200, { targets: [] });
    await listScanTargets();
    expect(calls[0].url).toContain('/api/scans/targets');
  });

  it('starts a scan with the chosen depth and budget', async () => {
    const calls = respond(202, { scan_id: 'scn_a', status: 'running' });
    await createScan({ port: 3000, mode: 'quick', budget_usd: 5 });
    expect(calls[0].method).toBe('POST');
    expect(JSON.parse(calls[0].body!)).toEqual({
      port: 3000,
      mode: 'quick',
      budget_usd: 5,
    });
  });

  it('sends a null budget through as "no cap" rather than dropping it', () => {
    const calls = respond(202, {});
    void createScan({ port: 3000, mode: 'deep', budget_usd: null });
    expect(JSON.parse(calls[0].body!).budget_usd).toBeNull();
  });

  it('stops and deletes through different verbs', async () => {
    let calls = respond(200, { ok: true, status: 'stopped' });
    await stopScan('scn_abc123def456');
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toContain('/stop');

    calls = respond(200, { ok: true, removed: true });
    await deleteScan('scn_abc123def456');
    expect(calls[0].method).toBe('DELETE');
    expect(calls[0].url).not.toContain('/stop');
  });

  it('escapes a finding id that carries punctuation', async () => {
    const calls = respond(200, { ok: true, dispositions: {} });
    await setFindingDisposition('scn_abc123def456', 'a/b:c', 'dismissed');
    expect(calls[0].url).toContain('a%2Fb%3Ac');
  });

  it('saves the connection and tests it', async () => {
    let calls = respond(200, {});
    await saveScanConnection({ model: 'prov/model', api_key: 'secret' });
    expect(calls[0].method).toBe('POST');
    expect(JSON.parse(calls[0].body!).model).toBe('prov/model');

    calls = respond(200, { ok: true, detail: '' });
    await testScanConnection();
    expect(calls[0].url).toContain('/api/scans/connection/test');
  });
});

describe('scan presentation helpers', () => {
  it('treats only a running scan as live', () => {
    expect(isLiveScan(scan({ status: 'running' }))).toBe(true);
    for (const status of ['done', 'stopped', 'failed', 'interrupted'] as const) {
      expect(isLiveScan(scan({ status }))).toBe(false);
    }
  });

  it('shows the time cost beside every depth', () => {
    for (const label of Object.values(MODE_LABELS)) {
      expect(label).toMatch(/minute|hour/);
    }
  });

  it('never reports a bare zero for a scan that could not run', () => {
    // Every failure mode leaves an empty findings list, identical to a clean
    // pass. Saying only "0" over one of those is the misleading case.
    expect(outcomeLabel(scan({ status: 'failed' }))).toMatch(/nothing was checked/i);
    expect(outcomeLabel(scan({ status: 'interrupted' }))).toMatch(/partly/i);
    expect(outcomeLabel(scan({ status: 'stopped' }))).toMatch(/partly/i);
  });

  it('says a clean scan finished, not just "0"', () => {
    const label = outcomeLabel(scan({ status: 'done' }));
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

  it('orders findings worst first and never hides an unknown severity', () => {
    const findings: Finding[] = [
      { id: 'c', severity: 'low' },
      { id: 'a', severity: 'made-up' },
      { id: 'b', severity: 'critical' },
    ];
    expect(sortFindings(findings).map((f) => f.id)).toEqual(['b', 'c', 'a']);
    expect(severityRank({ id: 'x' })).toBeGreaterThan(severityRank({ id: 'y', severity: 'low' }));
  });

  it('shows spend against a cap when there is one', () => {
    expect(spendLabel(scan())).toBe('$0.2310 of $5.00');
    expect(spendLabel(scan({ budget_usd: null }))).toBe('$0.2310');
  });

  it('formats ages and durations', () => {
    const now = 1_759_046_400_000;
    expect(agoLabel(1_759_046_400, now)).toBe('just now');
    expect(agoLabel(1_759_046_400 - 120, now)).toBe('2m ago');
    expect(durationLabel(scan(), now)).toBe('5m');
    expect(durationLabel(scan({ ended_at: null, started_at: 1_759_046_400 - 30 }), now)).toBe('30s');
  });

  it('surfaces fields the UI has no specific place for', () => {
    // A scanner version that adds a field must not have it silently dropped.
    const extras = extraFields({
      id: 'v1',
      severity: 'high',
      description: 'known',
      brand_new_field: 'something important',
    });
    expect(extras.map((e) => e.key)).toEqual(['brand_new_field']);
  });

  it('does not repeat fields that already have their own section', () => {
    const keys = extraFields({
      id: 'v1',
      title: 't',
      severity: 'high',
      description: 'd',
      impact: 'i',
      poc_script_code: 'curl',
      cvss: 9.1,
    }).map((e) => e.key);
    expect(keys).toEqual([]);
  });

  it('renders a structured extra field as readable text', () => {
    const extras = extraFields({ id: 'v1', odd: { nested: [1, 2] } });
    expect(extras[0].value).toContain('nested');
  });
});

describe('live scan page helpers', () => {
  it('titles a scan by its app, falling back to the port — never the address', () => {
    expect(scanTitle(scan())).toBe('shop');
    const unnamed = scan({ target: { port: 3000, name: '', url: 'http://172.17.0.1:3000' } });
    expect(scanTitle(unnamed)).toBe('Port 3000');
    expect(scanTitle(unnamed)).not.toContain('172.17');
  });

  it('says the depth as a phrase', () => {
    expect(modePhrase('quick')).toBe('Quick scan');
    expect(modePhrase('DEEP')).toBe('Deep scan');
    expect(modePhrase('')).toBe('Quick scan');
  });

  it('gives a clock time today and a date otherwise', () => {
    // In the viewer's own locale: 13:56 here, 1:56 PM in en-US.
    const at = new Date(2026, 9, 18, 13, 56);
    const time = at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    const started = at.getTime() / 1000;
    expect(startedLabel(started, new Date(2026, 9, 18, 14, 30))).toBe(`Started ${time}`);
    const older = startedLabel(started, new Date(2026, 9, 20, 9, 0));
    expect(older).toMatch(/^Started .*18.*, /);
    expect(older.endsWith(time)).toBe(true);
  });

  it('never rounds money being spent down to $0.00', () => {
    expect(moneyLabel(0)).toBe('$0.00');
    expect(moneyLabel(0.0042)).toBe('$0.0042');
    expect(moneyLabel(0.2906)).toBe('$0.29');
    expect(moneyLabel(12.5)).toBe('$12.50');
    expect(moneyLabel(Number.NaN)).toBe('$0.00');
  });

  it('measures the budget only when there is one', () => {
    expect(budgetFraction(scan({ budget_usd: 1, usage: { ...scan().usage, cost_usd: 0.29 } }))).toBeCloseTo(0.29);
    expect(budgetFraction(scan({ budget_usd: null }))).toBeNull();
    // A provider that overshoots the cap must not draw past the end of the bar.
    expect(budgetFraction(scan({ budget_usd: 1, usage: { ...scan().usage, cost_usd: 1.4 } }))).toBe(1);
  });

  it('never calls an empty list an all-clear for a scan that ended early', () => {
    expect(emptyFindingsText(scan({ status: 'done' }))).toBe(
      'No problems were recorded for this scan.',
    );
    for (const status of ['stopped', 'interrupted', 'failed'] as const) {
      const text = emptyFindingsText(scan({ status }));
      expect(text).toContain('None confirmed before the scan ended');
      expect(text).not.toContain('No problems were recorded');
    }
    expect(emptyFindingsText(scan({ status: 'running' }))).toContain('Nothing found yet');
  });

  it('names where a new report is saved, when the backend says', () => {
    expect(reportSavedText({ text: 'x', location: '/runs/r1/report.md' })).toBe(
      'The scanner’s report is ready. Saved to /runs/r1/report.md',
    );
    expect(reportSavedText({ text: 'x', location: '' })).toBe('The scanner’s report is ready.');
  });

  it('points at the report only when there is one', () => {
    // A scan that failed at start never writes a report.
    expect(emptyFindingsText(scan({ status: 'failed' }))).not.toContain('report');
    expect(
      emptyFindingsText({ status: 'stopped', report: { text: '# Summary', location: '' } }),
    ).toContain('The scanner’s report says what it did check.');
  });

  it('treats a finished-but-incomplete scan as not an all-clear', () => {
    // The user's case: the scan reached `done` having proven nothing,
    // because the scanner stopped testing near the spending limit and spent
    // what was left writing its report.
    const cut = scan({ status: 'done', coverage: { complete: false, caveats: ['cut short'] } });
    expect(emptyFindingsText(cut)).toContain('None confirmed before the scan ended');
    expect(outcomeLabel(cut)).toBe('Finished — but not all of the app was checked');
  });

  it('still says plainly when the scanner did cover the app', () => {
    const whole = scan({ status: 'done', coverage: { complete: true, caveats: [] } });
    expect(emptyFindingsText(whole)).toBe('No problems were recorded for this scan.');
    expect(outcomeLabel(whole)).toBe('Finished — nothing found');
  });

  it('never reads an older record with no verdict as a yes or a no', () => {
    // Absent coverage is unknown. It keeps the old wording rather than
    // inventing either an all-clear or a warning.
    expect(outcomeLabel(scan({ status: 'done' }))).toBe('Finished — nothing found');
  });
});

describe('how a finished scan ended', () => {
  it('says a stop the user asked for was theirs — never a crash', () => {
    const view = outcomeView(scan({ status: 'stopped', end_reason: 'user_stopped' }));
    expect(view.headline).toBe('You stopped this scan');
    expect(view.headline).not.toMatch(/unexpected/i);
    expect(view.tone).toBe('neutral');
    expect(view.action).toBe('again');
    expect(outcomeLabel(scan({ status: 'stopped', end_reason: 'user_stopped' }))).toBe(
      'Stopped by you — partly checked',
    );
  });

  it('offers the model settings only when the model is what failed', () => {
    const provider = outcomeView(scan({ status: 'failed', end_reason: 'provider_error' }));
    expect(provider).toMatchObject({ tone: 'danger', action: 'retry', settings: true });
    const scanner = outcomeView(scan({ status: 'failed', end_reason: 'scanner_error' }));
    expect(scanner).toMatchObject({ tone: 'danger', action: 'retry', settings: false });
  });

  it('names the spending limit in the headline', () => {
    const view = outcomeView(scan({ status: 'stopped', end_reason: 'budget_reached', budget_usd: 5 }));
    expect(view.headline).toBe('Reached its $5.00 spending limit');
    expect(outcomeLabel(scan({ status: 'stopped', end_reason: 'budget_reached' }))).toBe(
      'Hit its spending limit — partly checked',
    );
  });

  it('gives every end reason a headline and a next step to match', () => {
    const reasons = [
      'user_stopped',
      'budget_reached',
      'provider_error',
      'scanner_error',
      'process_vanished',
      'workspace_restarted',
      'lost_track',
    ] as const;
    for (const end_reason of reasons) {
      const view = outcomeView(scan({ status: 'failed', end_reason }));
      expect(view.headline, end_reason).toBeTruthy();
      expect(view.action, end_reason).not.toBeNull();
    }
  });

  it('reads a finished scan by its coverage, not just its status', () => {
    expect(outcomeView(scan({ status: 'done', end_reason: 'completed' }))).toMatchObject({
      tone: 'success',
      action: null,
    });
    const cut = scan({ status: 'done', end_reason: 'completed', coverage: { complete: false, caveats: [] } });
    expect(outcomeView(cut)).toMatchObject({ tone: 'warn', action: 'again' });
  });

  it('falls back on the status for a server too old to send a reason', () => {
    expect(outcomeView(scan({ status: 'failed' })).headline).toBe('The scan could not run');
    expect(outcomeView(scan({ status: 'interrupted' })).headline).toBe('The scan was cut short');
    expect(outcomeView(scan({ status: 'stopped' })).headline).toBe('The scan stopped early');
  });

  it('labels the status pill in words', () => {
    expect(STATUS_LABELS.interrupted).toBe('Cut short');
    expect(STATUS_LABELS.done).toBe('Finished');
  });

  it('retries the same scan without pinning the model that just failed', () => {
    const body = retryBody(
      scan({ mode: 'standard', budget_usd: 3, instruction: 'login page', model: 'old/model' }),
    );
    expect(body).toEqual({ port: 3000, mode: 'standard', budget_usd: 3, instruction: 'login page' });
    expect(body).not.toHaveProperty('model');
  });
});
