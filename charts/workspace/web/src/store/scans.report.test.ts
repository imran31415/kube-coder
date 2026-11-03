import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadScan, openScan } from './scans';
import { toasts } from './ui';
import * as api from '../api/scans';
import type { ScanDetail, ScanReport } from '../api/scans';

function detail(report?: ScanReport): ScanDetail {
  return {
    id: 'scn_A',
    status: report ? 'done' : 'running',
    target: { port: 3000, name: 'shop', url: 'http://10.0.0.1:3000' },
    findings: [],
    dispositions: {},
    ...(report ? { report } : {}),
  } as unknown as ScanDetail;
}

const REPORT = { text: '# Summary', location: '/runs/r1/report.md' };

describe('the report arriving', () => {
  beforeEach(() => {
    openScan.value = null;
    toasts.value = [];
  });
  afterEach(() => vi.restoreAllMocks());

  it('says where the report is saved when it lands with the scan on screen', async () => {
    vi.spyOn(api, 'getScan').mockResolvedValueOnce(detail()).mockResolvedValueOnce(detail(REPORT));
    await loadScan('scn_A');
    await loadScan('scn_A', true);
    expect(toasts.value.map((t) => t.message)).toEqual([
      'The scanner’s report is ready. Saved to /runs/r1/report.md',
    ]);
  });

  it('stays quiet when a finished scan is simply opened', async () => {
    // Its panel already says where the file is; a toast on every visit is noise.
    vi.spyOn(api, 'getScan').mockResolvedValue(detail(REPORT));
    await loadScan('scn_A');
    await loadScan('scn_A', true);
    expect(toasts.value).toEqual([]);
  });
});
