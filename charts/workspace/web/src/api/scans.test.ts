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
  durationLabel,
  extraFields,
  isLiveScan,
  outcomeLabel,
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
