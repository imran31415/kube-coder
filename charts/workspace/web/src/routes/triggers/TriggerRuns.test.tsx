import { render, screen, waitFor } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { TriggerRuns } from './TriggerRuns';
import type { Trigger, TriggerRun } from '../../api/triggers';

const listTriggerRuns = vi.fn();
vi.mock('../../api/triggers', async (orig) => ({
  ...(await orig<typeof import('../../api/triggers')>()),
  listTriggerRuns: (...a: unknown[]) => listTriggerRuns(...a),
}));

const navigate = vi.fn();
vi.mock('../../store/router', () => ({
  navigate: (...a: unknown[]) => navigate(...a),
  routeHref: (p: string) => `/oauth${p}`,
}));

const WEBHOOK: Trigger = {
  kind: 'webhook', id: 'gh-pr', name: 'gh-pr', prompt: 'Review the PR',
};
const CRON: Trigger = {
  kind: 'cron', id: 'nightly', name: 'nightly', prompt: 'Summarise',
  schedule: '0 2 * * *',
};
const WATCH: Trigger = {
  kind: 'page-watch', id: 'ci', name: 'ci', prompt: 'CI moved',
  url: 'https://example.test/badge',
};

function run(over: Partial<TriggerRun> = {}): TriggerRun {
  return {
    ts: 1700000000, type: 'webhook', trigger_id: 'gh-pr',
    outcome: 'spawned', ...over,
  } as TriggerRun;
}

function page(runs: TriggerRun[], total = runs.length, offset = 0) {
  return { runs, total, limit: 20, offset };
}

beforeEach(() => {
  listTriggerRuns.mockReset();
  navigate.mockReset();
  listTriggerRuns.mockResolvedValue(page([]));
});
afterEach(() => vi.restoreAllMocks());

describe('TriggerRuns', () => {
  it('asks the server for this trigger kind and id', async () => {
    render(<TriggerRuns t={WATCH} />);
    await waitFor(() =>
      expect(listTriggerRuns).toHaveBeenCalledWith('page-watch', 'ci',
                                                   { limit: 20, offset: 0 }));
  });

  it('renders one row per recorded run, newest first as served', async () => {
    listTriggerRuns.mockResolvedValue(page([
      run({ ts: 1700000200, task_id: 'tk-2' }),
      run({ ts: 1700000100, task_id: 'tk-1' }),
    ]));
    render(<TriggerRuns t={WEBHOOK} />);
    await waitFor(() => expect(screen.getByText('tk-2')).toBeInTheDocument());
    const links = screen.getAllByRole('link');
    expect(links.map((a) => a.textContent)).toEqual(['tk-2', 'tk-1']);
    expect(links[0]).toHaveAttribute('href', '/oauth/tasks/tk-2');
  });

  it('shows a rejected fire with its reason in plain English', async () => {
    listTriggerRuns.mockResolvedValue(page([
      run({ outcome: 'rejected', reason: 'bad_signature',
            signature_verified: false, error: 'signature verification failed' }),
    ]));
    render(<TriggerRuns t={WEBHOOK} />);
    await waitFor(() => expect(screen.getByText('rejected')).toBeInTheDocument());
    expect(screen.getByText('signature did not match')).toBeInTheDocument();
  });

  it('prints a reason once, not twice, when the error restates it', async () => {
    listTriggerRuns.mockResolvedValue(page([
      run({ outcome: 'rejected', reason: 'bad_token',
            error: 'fire token did not match' }),
    ]));
    render(<TriggerRuns t={CRON} />);
    await waitFor(() =>
      expect(screen.getAllByText('fire token did not match')).toHaveLength(1));
  });

  it('keeps an error that says more than the reason does', async () => {
    listTriggerRuns.mockResolvedValue(page([
      run({ outcome: 'rejected', reason: 'at_capacity',
            error: 'too many running tasks (12/12)' }),
    ]));
    render(<TriggerRuns t={CRON} />);
    expect(await screen.findByText('too many running tasks (12/12)'))
      .toBeInTheDocument();
  });

  it('marks a verified fire and an unverified one differently', async () => {
    listTriggerRuns.mockResolvedValue(page([
      run({ task_id: 'tk-2', signature_verified: true }),
      run({ task_id: 'tk-1', signature_verified: false, outcome: 'rejected',
            reason: 'bad_signature' }),
    ]));
    render(<TriggerRuns t={WEBHOOK} />);
    await waitFor(() => expect(screen.getByText('verified')).toBeInTheDocument());
    expect(screen.getByText('unverified')).toBeInTheDocument();
  });

  it('claims neither verified nor unverified when no check applied', async () => {
    listTriggerRuns.mockResolvedValue(page([run({ task_id: 'tk-1', manual: true })]));
    render(<TriggerRuns t={WEBHOOK} />);
    await waitFor(() => expect(screen.getByText('tk-1')).toBeInTheDocument());
    expect(screen.queryByText('verified')).not.toBeInTheDocument();
    expect(screen.queryByText('unverified')).not.toBeInTheDocument();
    expect(screen.getByText('dashboard')).toBeInTheDocument();
  });

  it('prefers the forwarded hop over the socket peer, and says which is which', async () => {
    listTriggerRuns.mockResolvedValue(page([
      run({ task_id: 'tk-1', source_ip: '10.0.0.1', forwarded_for: '203.0.113.9' }),
    ]));
    render(<TriggerRuns t={WEBHOOK} />);
    const cell = await screen.findByText('203.0.113.9');
    expect(cell.getAttribute('title')).toContain('asserted by the caller');
    expect(cell.getAttribute('title')).toContain('10.0.0.1');
  });

  it('routes in-app on a plain click of a task link', async () => {
    listTriggerRuns.mockResolvedValue(page([run({ task_id: 'tk-7' })]));
    render(<TriggerRuns t={WEBHOOK} />);
    await userEvent.click(await screen.findByText('tk-7'));
    expect(navigate).toHaveBeenCalledWith('/tasks/tk-7');
  });

  it('shows a run with no task (a rejection) without an empty link', async () => {
    listTriggerRuns.mockResolvedValue(page([
      run({ outcome: 'rejected', reason: 'at_capacity' }),
    ]));
    render(<TriggerRuns t={CRON} />);
    await waitFor(() =>
      expect(screen.getByText('workspace was at its task limit')).toBeInTheDocument());
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('explains the empty state in the vocabulary of the kind', async () => {
    render(<TriggerRuns t={WEBHOOK} />);
    expect(await screen.findByText(/Every POST to this webhook is recorded/))
      .toBeInTheDocument();
  });

  it('tells a page-watch owner that no-change checks are recorded too', async () => {
    render(<TriggerRuns t={WATCH} />);
    expect(await screen.findByText(/including the ones that found no change/))
      .toBeInTheDocument();
  });

  it('appends the next page instead of replacing the visible one', async () => {
    listTriggerRuns.mockResolvedValueOnce(page(
      Array.from({ length: 20 }, (_, i) => run({ task_id: `new-${i}` })), 25));
    listTriggerRuns.mockResolvedValueOnce(page(
      [run({ task_id: 'older-1' })], 25, 20));
    render(<TriggerRuns t={CRON} />);
    await waitFor(() => expect(screen.getByText('new-0')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /show older/i }));
    await waitFor(() => expect(screen.getByText('older-1')).toBeInTheDocument());
    expect(screen.getByText('new-0')).toBeInTheDocument();
    expect(listTriggerRuns).toHaveBeenLastCalledWith('cron', 'nightly',
                                                     { limit: 20, offset: 20 });
  });

  it('offers no pager when everything on disk is already shown', async () => {
    listTriggerRuns.mockResolvedValue(page([run({ task_id: 'tk-1' })]));
    render(<TriggerRuns t={CRON} />);
    await waitFor(() => expect(screen.getByText('tk-1')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /show older/i })).not.toBeInTheDocument();
  });

  it('reports a failed fetch instead of looking like an empty history', async () => {
    listTriggerRuns.mockRejectedValue(new Error('Unauthorized'));
    render(<TriggerRuns t={CRON} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Unauthorized');
  });

  it('renders an outcome the server adds later as itself', async () => {
    listTriggerRuns.mockResolvedValue(page([
      run({ outcome: 'quarantined' as TriggerRun['outcome'] }),
    ]));
    render(<TriggerRuns t={CRON} />);
    expect(await screen.findByText('quarantined')).toBeInTheDocument();
  });
});
