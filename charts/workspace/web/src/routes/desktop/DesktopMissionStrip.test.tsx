import { render, screen } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { DesktopMissionStrip } from './DesktopMissionStrip';
import { missionCards, missionPulse, stopMissionPolling } from '../../store/mission';
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
    evidence: [], parent_id: null, children: [],
    ...over,
  };
}

// Server order: waiting → running → done, newest first within each group.
const sample: MissionCard[] = [
  card({
    id: 'build:w1', ref_id: 'w1', state: 'waiting', title: 'Waiting build',
    waiting_since: now - 840,
    waiting_prompt: {
      kind: 'choice',
      question: 'Apply the migration?',
      options: [
        { index: 1, label: 'Yes' },
        { index: 2, label: 'No' },
      ],
    },
  }),
  card({ id: 'build:r1', ref_id: 'r1', state: 'running', title: 'Running build' }),
  card({ id: 'chat:r2', ref_id: 'r2', kind: 'chat', state: 'running', title: 'Running chat' }),
  card({
    id: 'build:d1', ref_id: 'd1', state: 'done', title: 'Done build',
    finished_at: now - 1200, outcome: { ok: true, detail: 'completed' },
  }),
  card({ id: 'build:d2', ref_id: 'd2', state: 'done', title: 'Overflow done build' }),
];

const samplePulse: MissionPulse = {
  running: 2, waiting: 1, done_today: 4, oldest_wait_s: 840, generated_at: now,
};

const realFetch = globalThis.fetch;

beforeEach(() => {
  missionCards.value = sample;
  missionPulse.value = samplePulse;
  // Quick replies are MutatorOnly — force the writable mode so they render.
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic' };
  // The strip starts polling on mount; feed the refresh the same payload so
  // it doesn't clobber the signals set above.
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

describe('DesktopMissionStrip', () => {
  it('renders the pulse counts and the top cards, waiting first, capped', () => {
    render(<DesktopMissionStrip />);
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.getByText('waiting on you')).toBeInTheDocument();
    expect(screen.getByText('done today')).toBeInTheDocument();

    const titles = Array.from(
      document.querySelectorAll('.dt-mc-title'),
    ).map((el) => el.textContent);
    expect(titles).toEqual(['Waiting build', 'Running build', 'Running chat', 'Done build']);
    // Capped at 4 — the fifth card stays on the full board only.
    expect(screen.queryByText('Overflow done build')).toBeNull();
  });

  it('shows quick replies on the waiting card', () => {
    render(<DesktopMissionStrip />);
    expect(screen.getByText('Apply the migration?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Yes' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'No' })).toBeInTheDocument();
  });

  it('links to the full Mission Control board', () => {
    render(<DesktopMissionStrip />);
    expect(screen.getByRole('button', { name: /View all/ })).toBeInTheDocument();
  });

  it('self-hides when the queue is empty', () => {
    missionCards.value = [];
    missionPulse.value = null;
    globalThis.fetch = vi.fn(async () => ({
      ok: true, status: 200,
      headers: { get: () => 'application/json' },
      json: async () => ({ cards: [], pulse: { ...samplePulse, running: 0 } }),
    })) as unknown as typeof fetch;
    const { container } = render(<DesktopMissionStrip />);
    expect(container.querySelector('.dt-section-mission')).toBeNull();
  });
});
