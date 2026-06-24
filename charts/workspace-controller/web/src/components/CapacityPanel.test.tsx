import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/preact';
import type { CapacityResponse } from '../api/capacity';

// The module is mocked with a PLAIN function (not a vi.fn spy) whose behaviour
// is steered by `respond`. A spy that returns a rejected promise gets its
// settled result tracked by vitest and, once the component's effect defers the
// catch past a test boundary, surfaces as a spurious failure — the plain
// function avoids that tracking entirely. See the deep-dive in git history.
let respond: () => Promise<CapacityResponse>;
vi.mock('../api/capacity', async (orig) => ({
  ...(await orig<typeof import('../api/capacity')>()),
  getCapacity: () => respond(),
}));

import { CapacityPanel } from './CapacityPanel';

const sample = (over: Partial<CapacityResponse> = {}): CapacityResponse => ({
  generatedAt: 1000,
  namespace: 'coder',
  cluster: {
    nodeCount: 2,
    cpu: { allocatable: 8, workspace: 1.5, cluster: 3, other: 1.5, workspacePct: 18.8, clusterPct: 37.5 },
    memory: { allocatable: 16e9, workspace: 3e9, cluster: 7e9, other: 4e9, workspacePct: 18.8, clusterPct: 43.8 },
    pods: { allocatable: 220, workspace: 3, cluster: 18 },
  },
  nodes: [
    {
      name: 'node-a',
      cpu: { allocatable: 4, workspace: 1, cluster: 2, other: 1, workspacePct: 25, clusterPct: 50 },
      memory: { allocatable: 8e9, workspace: 2e9, cluster: 4e9, other: 2e9, workspacePct: 25, clusterPct: 50 },
      pods: { allocatable: 110, workspace: 2, cluster: 10 },
    },
  ],
  history: {
    rangeSeconds: 3600,
    step: 60,
    cpu: { allocatable: [[1, 8], [2, 8]], workspace: [[1, 1], [2, 1.5]], cluster: [[1, 2], [2, 3]] },
    memory: { allocatable: [[1, 16e9], [2, 16e9]], workspace: [[1, 2e9], [2, 3e9]], cluster: [[1, 5e9], [2, 7e9]] },
  },
  metricsError: null,
  ...over,
});

describe('CapacityPanel', () => {
  beforeEach(() => {
    respond = () => Promise.resolve(sample());
  });

  it('renders the cluster summary and per-node breakdown', async () => {
    render(<CapacityPanel />);
    await waitFor(() => expect(screen.getByText(/2 nodes/)).toBeInTheDocument());
    expect(screen.getByText(/3 workspace pods of 18 scheduled/)).toBeInTheDocument();
    expect(screen.getByText('node-a')).toBeInTheDocument();
    // CPU utilisation surfaced in the summary header (37.5% -> rounds to 38%).
    expect(screen.getByText(/38% used/)).toBeInTheDocument();
  });

  it('surfaces a metricsError banner but still renders structure', async () => {
    respond = () => Promise.resolve(sample({ metricsError: 'prometheus unreachable' }));
    render(<CapacityPanel />);
    await waitFor(() =>
      expect(screen.getByText(/Live metrics unavailable: prometheus unreachable/)).toBeInTheDocument(),
    );
  });

  it('shows an error state when the request fails and there is no prior data', async () => {
    respond = () => Promise.reject(new Error('boom'));
    render(<CapacityPanel />);
    await waitFor(() => expect(screen.getByText(/Couldn't load capacity: boom/)).toBeInTheDocument());
  });

  it('renders an "unknown capacity" state when allocatable is absent', async () => {
    respond = () =>
      Promise.resolve(
        sample({
          cluster: {
            nodeCount: 1,
            cpu: { allocatable: null, workspace: 0.5, cluster: 0.5, other: 0, workspacePct: null, clusterPct: null },
            memory: { allocatable: null, workspace: 1e9, cluster: 1e9, other: 0, workspacePct: null, clusterPct: null },
            pods: { allocatable: null, workspace: 1, cluster: 5 },
          },
          nodes: [],
        }),
      );
    render(<CapacityPanel />);
    await waitFor(() => expect(screen.getAllByText(/capacity unknown/).length).toBeGreaterThan(0));
  });
});
