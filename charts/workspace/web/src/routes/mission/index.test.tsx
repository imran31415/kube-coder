import { render, screen } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { MissionRoute } from './index';
import {
  missionCards,
  missionPulse,
  missionError,
  missionFilter,
  missionKindFilter,
  stopMissionPolling,
} from '../../store/mission';
import { serverMode } from '../../store/server-mode';
import type { MissionCard, MissionPulse } from '../../api/mission';

const now = Math.floor(Date.now() / 1000);

function card(over: Partial<MissionCard>): MissionCard {
  return {
    id: 'build:1', ref_id: '1', kind: 'build', state: 'running',
    title: 'a build', headline: 'doing things',
    assistant: 'claude', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'main',
    created_at: now - 60, updated_at: now, finished_at: null,
    waiting_since: null, waiting_prompt: null, outcome: null,
    parent_id: null, children: [],
    ...over,
  };
}

// One card per column so every state renders.
const sample: MissionCard[] = [
  card({
    id: 'build:w1', ref_id: 'w1', state: 'waiting', title: 'Memory GC defaults',
    waiting_since: now - 840,
    waiting_prompt: {
      kind: 'choice',
      question: 'Run the purge command?',
      options: [
        { index: 1, label: 'Yes' },
        { index: 2, label: 'No' },
      ],
    },
  }),
  card({ id: 'build:r1', ref_id: 'r1', state: 'running', title: 'Trigger history' }),
  card({
    id: 'build:v1', ref_id: 'v1', state: 'done', title: 'Sidebar reorganization',
    finished_at: now - 1560, outcome: { ok: true, detail: 'completed' },
  }),
  card({
    id: 'chat:d1', ref_id: 'd1', kind: 'chat', state: 'done', title: 'Debug DinD TLS',
    repo: '', branch: '', outcome: { ok: true, detail: 'idle — resumable' },
  }),
];

const samplePulse: MissionPulse = {
  running: 1, waiting: 1, done_today: 2, oldest_wait_s: 840, generated_at: now,
};

const realFetch = globalThis.fetch;

beforeEach(() => {
  missionCards.value = sample;
  missionPulse.value = samplePulse;
  missionError.value = null;
  missionFilter.value = '';
  missionKindFilter.value = 'all';
  // Quick replies + Kill are MutatorOnly — force the writable mode so they render.
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic' };
  // MissionRoute starts polling on mount; feed the refresh the same payload
  // so it doesn't clobber the signals set above.
  globalThis.fetch = vi.fn(async () => ({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ cards: sample, pulse: samplePulse }),
  })) as unknown as typeof fetch;
});
afterEach(() => {
  stopMissionPolling();
  globalThis.fetch = realFetch;
});

describe('MissionRoute', () => {
  it('renders all three columns in priority order with cards bucketed by state', () => {
    render(<MissionRoute />);
    const cols = screen.getAllByRole('region');
    expect(cols.map((c) => c.getAttribute('aria-label'))).toEqual([
      'Waiting on you', 'Running', 'Done',
    ]);
    expect(screen.getByText('Memory GC defaults')).toBeInTheDocument();
    expect(screen.getByText('Trigger history')).toBeInTheDocument();
    expect(screen.getByText('Sidebar reorganization')).toBeInTheDocument();
    expect(screen.getByText('Debug DinD TLS')).toBeInTheDocument();
  });

  it('shows quick-reply buttons on the waiting card', () => {
    render(<MissionRoute />);
    expect(screen.getByText('Run the purge command?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Yes' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'No' })).toBeInTheDocument();
  });

  it('shows pulse counts', () => {
    render(<MissionRoute />);
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.getByText('waiting on you')).toBeInTheDocument();
    expect(screen.getByText('done today')).toBeInTheDocument();
    // oldest_wait_s=840 → "14m".
    expect(screen.getByText('14m')).toBeInTheDocument();
  });

  it('kind chips filter the board client-side', async () => {
    render(<MissionRoute />);
    screen.getByRole('button', { name: 'Chats' }).click();
    const { waitFor } = await import('@testing-library/preact');
    await waitFor(() => {
      expect(screen.queryByText('Trigger history')).toBeNull();
    });
    expect(screen.getByText('Debug DinD TLS')).toBeInTheDocument();
  });

  it('renders the empty state when there are no cards at all', () => {
    missionCards.value = [];
    missionPulse.value = null;
    globalThis.fetch = vi.fn(async () => ({
      ok: true, status: 200,
      headers: { get: () => 'application/json' },
      json: async () => ({ cards: [], pulse: { ...samplePulse, running: 0 } }),
    })) as unknown as typeof fetch;
    render(<MissionRoute />);
    expect(screen.getByText('No agents on the board')).toBeInTheDocument();
  });
});
