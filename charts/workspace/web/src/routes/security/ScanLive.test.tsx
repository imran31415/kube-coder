import { render, screen } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ScanDetail } from '../../api/scans';
import { serverMode } from '../../store/server-mode';

const store = vi.hoisted(() => ({
  remove: vi.fn(),
  stop: vi.fn(),
  rerun: vi.fn(),
  openModelSettings: vi.fn(),
}));
vi.mock('../../store/scans', () => store);

import { ScanLive } from './ScanLive';

const REPORT = { text: '## Executive summary', location: '/runs/r1/report.md' };

function scan(over: Partial<ScanDetail> = {}): ScanDetail {
  return {
    id: 'scn_abc123def456',
    status: 'done',
    backend: 'local-strix',
    target: { port: 3000, name: 'juice-shop', url: 'http://172.17.0.1:3000' },
    mode: 'quick',
    model: 'prov/model',
    budget_usd: 5,
    instruction: '',
    usage: { requests: 3, input_tokens: 0, output_tokens: 0, total_tokens: 0, cost_usd: 0.2 },
    counts: { critical: 0, high: 0, medium: 0, low: 0, other: 0, total: 0 },
    started_at: 1_759_046_400,
    ended_at: 1_759_046_700,
    error: null,
    findings: [],
    dispositions: {},
    summary: 'The scan of juice-shop finished.',
    end_reason: 'completed',
    ...over,
  };
}

describe('ScanLive report panel', () => {
  beforeEach(() => {
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  });

  it('opens the report beside the scan', () => {
    const { container } = render(<ScanLive scan={scan({ report: REPORT })} />);
    expect(screen.getByRole('complementary', { name: 'Scanner report' })).toBeTruthy();
    expect(container.querySelector('.scan-layout.has-report')).toBeTruthy();
  });

  it('can be hidden, and brought back from the header', async () => {
    const { container } = render(<ScanLive scan={scan({ report: REPORT })} />);
    await userEvent.click(screen.getByRole('button', { name: 'Hide report' }));
    expect(screen.queryByRole('complementary', { name: 'Scanner report' })).toBeNull();
    expect(container.querySelector('.has-report')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Show report' }));
    expect(screen.getByRole('complementary', { name: 'Scanner report' })).toBeTruthy();
  });

  it('has no panel and no button when the scanner wrote no report', () => {
    render(<ScanLive scan={scan()} />);
    expect(screen.queryByRole('complementary', { name: 'Scanner report' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Show report' })).toBeNull();
  });

  it('warns that deleting the scan deletes its saved report too', async () => {
    render(<ScanLive scan={scan({ report: REPORT })} />);
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(screen.getByText(/findings and its saved report will be removed/)).toBeTruthy();
  });
});
