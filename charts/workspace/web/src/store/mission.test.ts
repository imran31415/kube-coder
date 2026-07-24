import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import {
  missionCards,
  missionPulse,
  missionError,
  missionFilter,
  missionKindFilter,
  filteredMissionCards,
  refreshMission,
  startMissionPolling,
  stopMissionPolling,
} from './mission';
import type { MissionCard, MissionPulse } from '../api/mission';

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

const sample: MissionCard[] = [
  card({ id: 'build:1', ref_id: '1', title: 'Trigger history', branch: 'issue-91' }),
  card({ id: 'chat:t1', ref_id: 't1', kind: 'chat', state: 'done', title: 'Landing copy', repo: '', branch: '' }),
  card({ id: 'subagent:2', ref_id: '2', kind: 'subagent', title: 'test-writer', parent_id: 'build:1' }),
];

const samplePulse: MissionPulse = {
  running: 2, waiting: 0, done_today: 1, oldest_wait_s: 0, generated_at: now,
};

const realFetch = globalThis.fetch;

function mockQueue(cards: MissionCard[], pulse: MissionPulse) {
  const fn = vi.fn(async () => ({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ cards, pulse }),
  }));
  globalThis.fetch = fn as unknown as typeof fetch;
  return fn;
}

beforeEach(() => {
  missionCards.value = [];
  missionPulse.value = null;
  missionError.value = null;
  missionFilter.value = '';
  missionKindFilter.value = 'all';
});
afterEach(() => {
  stopMissionPolling();
  globalThis.fetch = realFetch;
  vi.useRealTimers();
});

describe('mission store', () => {
  it('refreshMission() populates cards and pulse', async () => {
    mockQueue(sample, samplePulse);
    await refreshMission();
    expect(missionCards.value).toHaveLength(3);
    expect(missionCards.value[0].title).toBe('Trigger history');
    expect(missionPulse.value?.running).toBe(2);
    expect(missionError.value).toBeNull();
  });

  it('refreshMission() records errors without clobbering cards', async () => {
    mockQueue(sample, samplePulse);
    await refreshMission();
    globalThis.fetch = vi.fn(async () => {
      throw new Error('boom');
    }) as unknown as typeof fetch;
    await refreshMission();
    expect(missionError.value).toBe('boom');
    expect(missionCards.value).toHaveLength(3);
  });

  it('filteredMissionCards honors kind + text filters', () => {
    missionCards.value = sample;
    expect(filteredMissionCards.value).toHaveLength(3);

    missionKindFilter.value = 'chat';
    expect(filteredMissionCards.value).toHaveLength(1);
    expect(filteredMissionCards.value[0].id).toBe('chat:t1');

    missionKindFilter.value = 'all';
    missionFilter.value = 'issue-91';
    expect(filteredMissionCards.value).toHaveLength(1);
    expect(filteredMissionCards.value[0].id).toBe('build:1');

    missionFilter.value = 'no-such-thing';
    expect(filteredMissionCards.value).toHaveLength(0);
  });

  it('startMissionPolling fetches immediately and on the interval; stop halts it', async () => {
    vi.useFakeTimers();
    const fn = mockQueue(sample, samplePulse);
    startMissionPolling(10000);
    // Immediate refresh on start.
    expect(fn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fn).toHaveBeenCalledTimes(2);
    stopMissionPolling();
    await vi.advanceTimersByTimeAsync(30000);
    expect(fn).toHaveBeenCalledTimes(2);
  });
});
