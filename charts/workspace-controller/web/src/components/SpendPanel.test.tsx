import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/preact';
import type { SpendResponse, TokenBlock } from '../api/spend';

// Plain function rather than a vi.fn spy, for the same reason as
// CapacityPanel.test.tsx: a tracked rejected promise settling after a test
// boundary shows up as a spurious failure.
let respond: () => Promise<SpendResponse>;
vi.mock('../api/spend', async (orig) => ({
  ...(await orig<typeof import('../api/spend')>()),
  getSpend: () => respond(),
}));

import { SpendPanel } from './SpendPanel';

const block = (over: Partial<TokenBlock> = {}): TokenBlock => ({
  input: 0,
  cache_read: 0,
  cache_write: 0,
  output: 0,
  total: 0,
  unclassified: 0,
  ...over,
});

const sample = (over: Partial<SpendResponse> = {}): SpendResponse => ({
  generatedAt: 1000,
  windowSeconds: 21600,
  fleet: {
    window: block({ input: 1_200_000, cache_read: 8_000_000, output: 90_000, total: 9_290_000 }),
    current: block({ input: 3_000_000, total: 3_000_000 }),
  },
  byWorkspace: [
    { user: 'alice', namespace: 'ws-alice', window: block({ input: 1_000_000, total: 1_000_000 }), current: block() },
    { user: 'bob', namespace: 'ws-bob', window: block({ input: 200_000, total: 200_000 }), current: block() },
  ],
  byModel: [
    { model: 'claude-opus-5', window: block({ input: 900_000, total: 900_000 }), current: block() },
  ],
  coverage: {
    window: { measured: 8, not_instrumented: 3, no_session_id: 1 },
    current: { measured: 20, not_instrumented: 5, no_session_id: 1 },
  },
  workspacesReporting: 2,
  metricsError: null,
  scrapeHint: null,
  ...over,
});

describe('SpendPanel', () => {
  beforeEach(() => {
    respond = () => Promise.resolve(sample());
  });

  it('renders the fleet total with its per-class split and both breakdowns', async () => {
    render(<SpendPanel />);
    await waitFor(() => expect(screen.getByText('9.3M')).toBeInTheDocument());
    expect(screen.getByText('2 workspaces reporting')).toBeInTheDocument();
    expect(screen.getByText('Cache read')).toBeInTheDocument();
    expect(screen.getByText('alice')).toBeInTheDocument();
    expect(screen.getByText('bob')).toBeInTheDocument();
    expect(screen.getByText('claude-opus-5')).toBeInTheDocument();
  });

  it('states how much of the fleet was measurable at all', async () => {
    render(<SpendPanel />);
    await waitFor(() => expect(screen.getByText(/8 of 12 runs measurable/)).toBeInTheDocument());
    expect(screen.getByText(/spend is unknown, not zero/)).toBeInTheDocument();
  });

  it('says spend metrics require Prometheus rather than showing zeroes', async () => {
    respond = () =>
      Promise.resolve(sample({ metricsError: 'metrics disabled (PROMETHEUS_URL unset)' }));
    render(<SpendPanel />);
    await waitFor(() =>
      expect(screen.getByText(/Spend metrics require Prometheus/)).toBeInTheDocument(),
    );
    // The numbers are absent, not zero — so none of them are rendered.
    expect(screen.queryByText('Cache read')).not.toBeInTheDocument();
  });

  it('distinguishes a scrape gap from a fleet that spent nothing', async () => {
    respond = () =>
      Promise.resolve(
        sample({
          workspacesReporting: 0,
          byWorkspace: [],
          scrapeHint: 'nothing scrapes /metrics/prometheus by default',
        }),
      );
    render(<SpendPanel />);
    await waitFor(() =>
      expect(screen.getByText(/No workspace is exporting spend metrics/)).toBeInTheDocument(),
    );
  });

  it('shows an error state when the request fails outright', async () => {
    respond = () => Promise.reject(new Error('boom'));
    render(<SpendPanel />);
    await waitFor(() =>
      expect(screen.getByText(/Couldn't load agent spend: boom/)).toBeInTheDocument(),
    );
  });

  it('flags unclassified tokens beside the total instead of inside it', async () => {
    respond = () =>
      Promise.resolve(
        sample({
          fleet: { window: block({ input: 1000, total: 1000, unclassified: 500 }), current: block() },
        }),
      );
    render(<SpendPanel />);
    await waitFor(() => expect(screen.getByText(/500 unclassified/)).toBeInTheDocument());
  });
});
