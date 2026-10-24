import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/preact';
import type { Workspace, WorkspaceState, WorkspacesResponse } from './api/workspaces';
import type { ClusterHealthResponse } from './api/capacity';

// Plain-function module mocks (not vi.fn spies) for the same reason
// CapacityPanel/ProvisionForm use them: a spy tracks deferred promise settles
// and can surface them as spurious cross-test failures. Behaviour is steered by
// the module-level `rows` / call logs below.
let rows: Workspace[] = [];
const started: string[] = [];
const stopped: string[] = [];
vi.mock('./api/workspaces', async (orig) => ({
  ...(await orig<typeof import('./api/workspaces')>()),
  listWorkspaces: (): Promise<WorkspacesResponse> =>
    Promise.resolve({ namespace: 'coder', workspaces: rows, latestVersion: 'v1.5.0' }),
  startWorkspace: (user: string) => {
    started.push(user);
    return Promise.resolve({ ok: true as const, user, desiredReplicas: 1 });
  },
  stopWorkspace: (user: string) => {
    stopped.push(user);
    return Promise.resolve({ ok: true as const, user, desiredReplicas: 0 });
  },
  // No metrics in tests: the detail page then falls back to the controller's
  // own state, which is the path that has to tell sleeping from stopped.
  getWorkspaceMetrics: () => Promise.reject(new Error('no metrics in tests')),
}));
vi.mock('./api/provision', async (orig) => ({
  ...(await orig<typeof import('./api/provision')>()),
  getProvisionConfig: () => Promise.resolve({ enabled: false, workspaceDomain: '', oauthAppNewUrl: '' }),
}));
vi.mock('./api/capacity', async (orig) => ({
  ...(await orig<typeof import('./api/capacity')>()),
  getCapacitySummary: (): Promise<ClusterHealthResponse> =>
    Promise.resolve({
      generatedAt: 1000,
      namespace: 'coder',
      cluster: null,
      status: 'unknown',
      metricsError: null,
    }),
}));

import { App } from './app';
import { route } from './router';
import { workspaces, loaded, error } from './store';

const ws = (user: string, state: WorkspaceState, over: Partial<Workspace> = {}): Workspace => ({
  user,
  deployment: `ws-${user}`,
  namespace: `ws-${user}`,
  isolated: true,
  state,
  desiredReplicas: state === 'stopped' || state === 'sleeping' ? 0 : 1,
  readyReplicas: state === 'running' ? 1 : 0,
  url: null,
  pods: [],
  detail: state === 'stopped' ? 'scaled to 0' : state === 'sleeping' ? 'asleep' : '1/1 ready',
  image: null,
  imageTag: null,
  version: 'v1.5.0',
  updateAvailable: false,
  ...over,
});

/** The <li> for a workspace, so per-row buttons aren't ambiguous across rows. */
const rowFor = (user: string) => screen.getByText(user).closest('li') as HTMLElement;

const chip = (name: string) => screen.getByRole('button', { name });

describe('WorkspaceList state filter (#547)', () => {
  beforeEach(() => {
    location.hash = '#/';
    route.value = '/';
    // Seed the signals directly too: the poll is async, but the first paint
    // should already reflect the default filter.
    rows = [
      ws('alice', 'running'),
      ws('bob', 'stopped'),
      ws('carol', 'degraded'),
      ws('dave', 'stopped'),
      ws('erin', 'transitioning'),
      ws('frank', 'stopped', { isolated: false, namespace: 'coder' }),
    ];
    workspaces.value = rows;
    loaded.value = true;
    error.value = null;
    started.length = 0;
    stopped.length = 0;
  });

  it('defaults to running-only and labels each chip with its count', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // running/degraded/transitioning all count as active; stopped is the split.
    expect(chip('Running (3)')).toHaveAttribute('aria-pressed', 'true');
    expect(chip('Stopped (3)')).toHaveAttribute('aria-pressed', 'false');
    expect(chip('All (6)')).toHaveAttribute('aria-pressed', 'false');

    expect(screen.getByText('carol')).toBeInTheDocument();
    expect(screen.getByText('erin')).toBeInTheDocument();
    expect(screen.queryByText('bob')).not.toBeInTheDocument();
    expect(screen.queryByText('dave')).not.toBeInTheDocument();
    expect(screen.getByText(/1–3 of 3 \(filtered from 6\)/)).toBeInTheDocument();
  });

  it('switching to Stopped reveals the stopped workspaces and hides the running ones', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(chip('Stopped (3)'));

    expect(chip('Stopped (3)')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('bob')).toBeInTheDocument();
    expect(screen.getByText('dave')).toBeInTheDocument();
    expect(screen.getByText('frank')).toBeInTheDocument();
    expect(screen.queryByText('alice')).not.toBeInTheDocument();
  });

  it('All shows every workspace regardless of state', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(chip('All (6)'));

    for (const u of ['alice', 'bob', 'carol', 'dave', 'erin', 'frank']) {
      expect(screen.getByText(u)).toBeInTheDocument();
    }
  });

  it('keeps a stopped workspace startable — its Start button still works', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(chip('Stopped (3)'));
    const start = within(rowFor('bob')).getByRole('button', { name: 'Start' });
    fireEvent.click(start);

    await waitFor(() => expect(started).toEqual(['bob']));
    // Starting must not route through the stop confirmation.
    expect(stopped).toEqual([]);
  });

  it('counts are faceted: search and namespace chips narrow them, the state chip does not', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // frank is the only shared-namespace workspace, and it is stopped.
    fireEvent.click(screen.getByRole('button', { name: 'Shared' }));
    expect(chip('Running (0)')).toBeInTheDocument();
    expect(chip('Stopped (1)')).toBeInTheDocument();
    expect(chip('All (1)')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Any ns' }));
    fireEvent.input(screen.getByLabelText('Search workspaces'), { target: { value: 'alice' } });
    expect(chip('Running (1)')).toBeInTheDocument();
    expect(chip('Stopped (0)')).toBeInTheDocument();
  });

  it('offers a way back when the filter empties the list, so nothing looks vanished', async () => {
    rows = [ws('bob', 'stopped'), ws('dave', 'stopped')];
    workspaces.value = rows;
    render(<App />);
    await waitFor(() => expect(chip('Running (0)')).toBeInTheDocument());

    expect(screen.getByText('No running workspaces.')).toBeInTheDocument();
    expect(screen.queryByText('bob')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show all 2 workspaces' }));

    expect(screen.getByText('bob')).toBeInTheDocument();
    expect(screen.getByText('dave')).toBeInTheDocument();
    expect(chip('All (2)')).toHaveAttribute('aria-pressed', 'true');
  });

  it('falls back to the plain empty message when no state is being hidden', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.input(screen.getByLabelText('Search workspaces'), { target: { value: 'nobody' } });

    expect(screen.getByText('No workspaces match your search or filters.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Show all/ })).not.toBeInTheDocument();
  });
});

describe('Sleeping workspaces (#728 step 5)', () => {
  const asked: string[] = [];
  let answer = true;
  const realConfirm = window.confirm;

  beforeEach(() => {
    location.hash = '#/';
    route.value = '/';
    rows = [
      ws('alice', 'running'),
      ws('gus', 'sleeping'),
      ws('bob', 'stopped'),
    ];
    workspaces.value = rows;
    loaded.value = true;
    error.value = null;
    started.length = 0;
    stopped.length = 0;
    asked.length = 0;
    answer = true;
    window.confirm = (message?: string) => {
      asked.push(message ?? '');
      return answer;
    };
    return () => {
      window.confirm = realConfirm;
    };
  });

  it('gets a chip of its own, counted as neither running nor stopped', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    expect(chip('Running (1)')).toHaveAttribute('aria-pressed', 'true');
    expect(chip('Sleeping (1)')).toHaveAttribute('aria-pressed', 'false');
    expect(chip('Stopped (1)')).toBeInTheDocument();
    expect(chip('All (3)')).toBeInTheDocument();
    // Resting costs nothing, so the default "what is running" view leaves it out.
    expect(screen.queryByText('gus')).not.toBeInTheDocument();
  });

  it('says it is asleep, and offers Stop rather than Start', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    fireEvent.click(chip('Sleeping (1)'));

    const row = rowFor('gus');
    expect(within(row).getByText('sleeping')).toHaveClass('pill', 'pill-sleeping');
    expect(within(row).getByText(/ws-gus · asleep/)).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Stop' })).toHaveAttribute(
      'title',
      'Stop workspace — a visit will no longer wake it',
    );
    expect(within(row).queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();
  });

  it('Stop says what it changes for a sleeping workspace, then stops it', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    fireEvent.click(chip('Sleeping (1)'));

    fireEvent.click(within(rowFor('gus')).getByRole('button', { name: 'Stop' }));

    await waitFor(() => expect(stopped).toEqual(['gus']));
    expect(asked).toHaveLength(1);
    expect(asked[0]).toContain('It is asleep now, so a visit would wake it.');
    expect(asked[0]).toContain('stays off until someone starts it here');
    expect(asked[0]).not.toContain('compute freed'); // there is no pod to free
  });

  it('declining the question stops nothing', async () => {
    answer = false;
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    fireEvent.click(chip('Sleeping (1)'));

    fireEvent.click(within(rowFor('gus')).getByRole('button', { name: 'Stop' }));

    expect(asked).toHaveLength(1);
    expect(stopped).toEqual([]);
  });

  it('a running workspace keeps the old question', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(within(rowFor('alice')).getByRole('button', { name: 'Stop' }));

    await waitFor(() => expect(stopped).toEqual(['alice']));
    expect(asked[0]).toContain('compute freed');
  });

  it('the detail page says asleep, not stopped and not running', async () => {
    location.hash = '#/w/gus';
    route.value = '/w/gus';
    render(<App />);

    await waitFor(() => expect(screen.getByText(/ws-gus · asleep/)).toBeInTheDocument());
    expect(screen.queryByText(/· stopped/)).not.toBeInTheDocument();
    expect(screen.queryByText(/· running/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
  });
});

