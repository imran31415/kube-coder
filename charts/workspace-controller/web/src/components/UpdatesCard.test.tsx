import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import type { Workspace, WorkspaceMetrics, UpdateResult } from '../api/workspaces';

// Plain-function module mock (matching ProvisionForm.test) so deferred-promise
// settling can't surface spurious cross-test failures. `updated` records calls.
const updated: { user: string }[] = [];
let metrics: () => Promise<WorkspaceMetrics>;
vi.mock('../api/workspaces', async (orig) => ({
  ...(await orig<typeof import('../api/workspaces')>()),
  getWorkspaceMetrics: () => metrics(),
  updateWorkspace: (user: string) => {
    updated.push({ user });
    return Promise.resolve({
      ok: true,
      user,
      fromVersion: 'v1.3.0',
      toVersion: 'v1.4.0',
      imageTag: 'devlaptop-v1.4.0',
      image: 'registry/coder:devlaptop-v1.4.0',
      rolled: true,
      persisted: true,
      persistError: null,
    } as UpdateResult);
  },
}));

import { WorkspaceDetail } from './WorkspaceDetail';
import { workspaces, latestVersion } from '../store';

const sampleMetrics = (): WorkspaceMetrics => ({
  user: 'octo',
  running: true,
  cpu: { cores: 0.1, limitCores: 2, pct: 5 },
  memory: { bytes: 1e9, limitBytes: 4e9, pct: 25 },
  disk: { usedBytes: 1e9, capacityBytes: 5e10, pct: 2 },
  network: { rxBps: 0, txBps: 0 },
  uptimeSeconds: 60,
  cost: null,
  spark: { rangeSeconds: 3600, step: 60, cpu: [], memory: [], disk: [] },
  metricsError: null,
});

const ws = (over: Partial<Workspace> = {}): Workspace => ({
  user: 'octo',
  deployment: 'ws-octo',
  state: 'running',
  desiredReplicas: 1,
  readyReplicas: 1,
  url: null,
  pods: [],
  detail: '1/1 ready',
  image: 'registry/coder:devlaptop-v1.3.0',
  imageTag: 'devlaptop-v1.3.0',
  version: 'v1.3.0',
  updateAvailable: true,
  ...over,
});

describe('UpdatesCard (in WorkspaceDetail)', () => {
  beforeEach(() => {
    updated.length = 0;
    metrics = () => Promise.resolve(sampleMetrics());
    latestVersion.value = 'v1.4.0';
    workspaces.value = [ws()];
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });

  it('offers an enabled update when a newer release exists', async () => {
    render(<WorkspaceDetail user="octo" />);
    const btn = await screen.findByText('Restart & update');
    expect((btn as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText('→ v1.4.0')).toBeInTheDocument();
  });

  it('calls updateWorkspace and shows the rollout note on click', async () => {
    render(<WorkspaceDetail user="octo" />);
    const btn = await screen.findByText('Restart & update');
    fireEvent.click(btn);
    await waitFor(() => expect(updated).toEqual([{ user: 'octo' }]));
    await waitFor(() =>
      expect(screen.getByText(/Pod is rolling out \(pinned in GitOps\)/)).toBeInTheDocument(),
    );
  });

  it('shows "Up to date" (disabled) when already on the latest', async () => {
    workspaces.value = [ws({ version: 'v1.4.0', updateAvailable: false })];
    render(<WorkspaceDetail user="octo" />);
    const btn = await screen.findByText('Up to date');
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });
});
