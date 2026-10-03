import { describe, expect, it, beforeEach, vi, afterEach } from 'vitest';
import { loadScan, openScan, openScanError } from './scans';
import * as api from '../api/scans';
import type { ScanDetail } from '../api/scans';

function detail(id: string): ScanDetail {
  return {
    id,
    status: 'running',
    mode: 'quick',
    model: 'prov/model',
    target: { port: 3000, name: 'shop', addr: '0.0.0.0', reachable: true, reason: '' },
    findings: [],
    dispositions: {},
    counts: { critical: 0, high: 0, medium: 0, low: 0, other: 0, total: 0 },
    usage: {},
    created_at: 0,
    ended_at: null,
    error: '',
    budget_usd: null,
    instruction: '',
  } as unknown as ScanDetail;
}

describe('loadScan stale-response guard', () => {
  beforeEach(() => {
    openScan.value = null;
    openScanError.value = null;
  });
  afterEach(() => vi.restoreAllMocks());

  it('does not let a slow reply for the previous scan overwrite the current one', async () => {
    // Without the guard this left the detail route on "Loading…" forever:
    // the render requires scan.id === scanId, and nothing refetches B
    // afterwards because every refresh path reads openScan.value.id (= A).
    const resolvers: Record<string, (d: ScanDetail) => void> = {};
    vi.spyOn(api, 'getScan').mockImplementation(
      (id: string) =>
        new Promise<ScanDetail>((resolve) => {
          resolvers[id] = resolve;
        }),
    );

    const first = loadScan('scn_A');
    const second = loadScan('scn_B');

    resolvers['scn_B'](detail('scn_B'));
    await second;
    resolvers['scn_A'](detail('scn_A'));
    await first;

    expect(openScan.value?.id).toBe('scn_B');
  });

  it('drops a stale error as well as a stale success', async () => {
    const resolvers: Record<string, () => void> = {};
    const rejecters: Record<string, (e: Error) => void> = {};
    vi.spyOn(api, 'getScan').mockImplementation(
      (id: string) =>
        new Promise<ScanDetail>((resolve, reject) => {
          resolvers[id] = () => resolve(detail(id));
          rejecters[id] = reject;
        }),
    );

    const first = loadScan('scn_A');
    const second = loadScan('scn_B');
    resolvers['scn_B']();
    await second;
    rejecters['scn_A'](new Error('gone'));
    await first;

    expect(openScan.value?.id).toBe('scn_B');
    expect(openScanError.value).toBeNull();
  });
});
