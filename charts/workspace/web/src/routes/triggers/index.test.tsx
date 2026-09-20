import { render, screen, waitFor } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { TriggersRoute } from './index';
import { triggers, triggerFilter, stopTriggerPolling } from '../../store/triggers';
import type { Trigger } from '../../api/triggers';

/**
 * The row-level half of the run-history feature (#91): the panel has to be
 * REACHABLE, and it has to be collapsed until asked for. The second is the
 * load-bearing one — mounting it eagerly would turn opening the Triggers tab
 * into one ledger fetch per row.
 */

const { listTriggerRuns } = vi.hoisted(() => ({ listTriggerRuns: vi.fn() }));
vi.mock('../../api/triggers', async (orig) => ({
  ...(await orig<typeof import('../../api/triggers')>()),
  listTriggerRuns: (...a: unknown[]) => listTriggerRuns(...a),
}));

const SAMPLE: Trigger[] = [
  { kind: 'webhook', id: 'gh-pr', name: 'gh-pr', prompt: 'Review the PR' },
  { kind: 'cron', id: 'nightly', name: 'nightly', prompt: 'Summarise',
    schedule: '0 2 * * *' },
];

const realFetch = globalThis.fetch;

beforeEach(() => {
  listTriggerRuns.mockReset();
  listTriggerRuns.mockResolvedValue({ runs: [], total: 0, limit: 20, offset: 0 });
  triggers.value = SAMPLE;
  triggerFilter.value = '';
  // The route starts polling on mount, and that refresh REPLACES the seeded
  // signal — so the stub has to answer with the same two triggers, per
  // collection, or the list empties itself mid-test.
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes('/api/webhooks')
      ? { webhooks: [{ id: 'gh-pr', prompt_template: 'Review the PR' }] }
      : url.includes('/api/crons')
        ? { crons: [{ id: 'nightly', schedule: '0 2 * * *',
                      prompt_template: 'Summarise' }] }
        : { page_watches: [] };
    return {
      ok: true, status: 200,
      headers: { get: () => 'application/json' },
      json: async () => body,
    };
  }) as unknown as typeof fetch;
});

afterEach(() => {
  stopTriggerPolling();
  globalThis.fetch = realFetch;
  triggers.value = [];
});

describe('Triggers list — run history', () => {
  it('fetches no run history until a row is opened', () => {
    render(<TriggersRoute />);
    expect(screen.getAllByRole('button', { name: 'Runs' })).toHaveLength(2);
    expect(listTriggerRuns).not.toHaveBeenCalled();
  });

  it('loads only the opened row\'s ledger', async () => {
    render(<TriggersRoute />);
    await userEvent.click(screen.getAllByRole('button', { name: 'Runs' })[1]);
    await waitFor(() => expect(listTriggerRuns).toHaveBeenCalledTimes(1));
    expect(listTriggerRuns).toHaveBeenCalledWith('cron', 'nightly',
                                                 { limit: 20, offset: 0 });
  });

  it('shows the panel, then hides it again', async () => {
    render(<TriggersRoute />);
    const toggle = screen.getAllByRole('button', { name: 'Runs' })[0];
    await userEvent.click(toggle);
    expect(await screen.findByLabelText('Run history for gh-pr')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Hide runs' }));
    expect(screen.queryByLabelText('Run history for gh-pr')).not.toBeInTheDocument();
  });
});
