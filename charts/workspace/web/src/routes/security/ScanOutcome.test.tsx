import { render, screen } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ScanDetail } from '../../api/scans';
import { serverMode } from '../../store/server-mode';

const store = vi.hoisted(() => ({ rerun: vi.fn(), openModelSettings: vi.fn() }));
vi.mock('../../store/scans', () => store);

import { ScanOutcome } from './ScanOutcome';

function scan(over: Partial<ScanDetail> = {}): ScanDetail {
  return {
    id: 'scn_abc123def456',
    status: 'failed',
    backend: 'local-strix',
    target: { port: 3000, name: 'juice-shop', url: 'http://172.17.0.1:3000' },
    mode: 'quick',
    model: 'prov/model',
    budget_usd: 5,
    instruction: '',
    usage: { requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, cost_usd: 0 },
    counts: { critical: 0, high: 0, medium: 0, low: 0, other: 0, total: 0 },
    started_at: 1_759_046_400,
    ended_at: 1_759_046_409,
    error: null,
    findings: [],
    dispositions: {},
    summary: 'The scan of juice-shop could not run, so nothing was checked.',
    ...over,
  };
}

describe('ScanOutcome', () => {
  beforeEach(() => {
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
    store.rerun.mockReset();
    store.openModelSettings.mockReset();
  });

  it('says why a failed scan failed, and offers the fix', async () => {
    const user = userEvent.setup();
    render(
      <ScanOutcome
        scan={scan({
          end_reason: 'provider_error',
          error: 'The provider is busy right now. Wait a moment and try again.',
        })}
      />,
    );
    expect(screen.getByRole('heading', { name: 'Your AI model could not be used' })).toBeInTheDocument();
    expect(screen.getByText(/The provider is busy right now/)).toBeInTheDocument();
    expect(screen.getByText(/could not run, so nothing was checked/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(store.rerun).toHaveBeenCalledWith(expect.objectContaining({ id: 'scn_abc123def456' }));
    await user.click(screen.getByRole('button', { name: 'Model settings' }));
    expect(store.openModelSettings).toHaveBeenCalled();
  });

  it('never calls a stop the user asked for unexpected', () => {
    render(
      <ScanOutcome
        scan={scan({
          status: 'stopped',
          end_reason: 'user_stopped',
          summary: 'You stopped the scan of juice-shop before it finished, so only part of the app was checked.',
        })}
      />,
    );
    expect(screen.getByRole('heading', { name: 'You stopped this scan' })).toBeInTheDocument();
    expect(screen.queryByText(/unexpected/i)).toBeNull();
    expect(screen.getByRole('button', { name: 'Scan again' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Model settings' })).toBeNull();
  });

  it('offers nothing to click on a clean finish', () => {
    render(
      <ScanOutcome
        scan={scan({ status: 'done', end_reason: 'completed', summary: 'A quick scan of juice-shop finished and found nothing.' })}
      />,
    );
    expect(screen.getByRole('heading', { name: 'Scan finished' })).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('lists what the scanner says it did not reach', () => {
    render(
      <ScanOutcome
        scan={scan({
          status: 'done',
          end_reason: 'completed',
          coverage: { complete: false, caveats: ['The run terminated via budget_exceeded.'] },
          caveats: ['The run terminated via budget_exceeded.'],
        })}
      />,
    );
    expect(screen.getByRole('heading', { name: 'Finished, but not all of the app was checked' })).toBeInTheDocument();
    expect(screen.getByText('The run terminated via budget_exceeded.')).toBeInTheDocument();
  });

  it('hides the buttons from the read-only demo', () => {
    serverMode.value = { readOnly: true, authed: true, authMode: 'basic', demoShowAll: false };
    render(<ScanOutcome scan={scan({ end_reason: 'scanner_error', error: 'Docker not installed.' })} />);
    expect(screen.getByText('Docker not installed.')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });
});
