import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { HypervisorThread, HypervisorConfig } from '../api/hypervisor';

// Reasoning-effort selector store logic (#362) in isolation — no network.
const { setThreadEffort, listThreads } = vi.hoisted(() => ({
  setThreadEffort: vi.fn(),
  listThreads: vi.fn(),
}));

vi.mock('../api/hypervisor', () => ({
  setThreadEffort: (...a: unknown[]) => setThreadEffort(...a),
  listThreads: (...a: unknown[]) => listThreads(...a),
  // Unused-by-these-tests exports the store imports at module load.
  renameThread: vi.fn(),
  setThreadModel: vi.fn(),
  createThread: vi.fn(),
  getThread: vi.fn(),
  getHypervisorConfig: vi.fn(),
  sendThreadMessage: vi.fn(),
  stopThread: vi.fn(),
  deleteThread: vi.fn(),
}));
vi.mock('../api/tasks', () => ({ listTasks: vi.fn() }));
vi.mock('./router', () => ({
  navigate: vi.fn(),
  currentPath: { value: '/hypervisor' },
}));

import {
  threads,
  config,
  activeThreadId,
  selectedEffort,
  assistantEfforts,
  assistantEffortDefault,
  assistantEffortCap,
  setSelectedAssistant,
  setActiveThreadEffort,
} from './hypervisor';

function thread(over: Partial<HypervisorThread> = {}): HypervisorThread {
  return { id: 'a', title: 't', assistant: 'claude', status: 'idle',
    created_at: 1, updated_at: 1, ...over };
}

function cfg(): HypervisorConfig {
  return {
    enabled: true,
    defaultAssistant: 'claude',
    workdir: '/home/dev',
    readOnly: false,
    assistants: [
      { id: 'claude', label: 'Claude Code', models: ['default'],
        efforts: ['low', 'medium', 'high', 'xhigh', 'max'], effort: 'high', effortCap: 'xhigh' },
      { id: 'kc-harness', label: 'Harness', models: [],
        efforts: ['low', 'medium', 'high', 'xhigh', 'max'], effort: 'high', effortCap: 'high' },
      { id: 'librefang', label: 'LibreFang', models: [], efforts: [] },
    ],
  };
}

beforeEach(() => {
  setThreadEffort.mockReset();
  listThreads.mockReset().mockResolvedValue([]);
  config.value = cfg();
  threads.value = [thread({ id: 'a' })];
  activeThreadId.value = null;
  selectedEffort.value = '';
});

describe('effort config helpers', () => {
  it('exposes the 5-stop axis + cap for a supported assistant', () => {
    expect(assistantEfforts('claude')).toEqual(['low', 'medium', 'high', 'xhigh', 'max']);
    expect(assistantEffortDefault('claude')).toBe('high');
    expect(assistantEffortCap('claude')).toBe('xhigh');
    expect(assistantEffortCap('kc-harness')).toBe('high');
  });

  it('reports no efforts for a knob-less assistant (selector hidden)', () => {
    expect(assistantEfforts('librefang')).toEqual([]);
    expect(assistantEffortDefault('librefang')).toBe('');
    expect(assistantEffortCap('librefang')).toBe('');
  });
});

describe('setSelectedAssistant resets effort to the assistant default', () => {
  it('seeds the new assistant default', () => {
    setSelectedAssistant('claude');
    expect(selectedEffort.value).toBe('high');
    setSelectedAssistant('librefang');
    expect(selectedEffort.value).toBe(''); // hidden → no default
  });
});

describe('setActiveThreadEffort', () => {
  it('updates the new-chat default when no thread is open', async () => {
    activeThreadId.value = null;
    await setActiveThreadEffort('low');
    expect(selectedEffort.value).toBe('low');
    expect(setThreadEffort).not.toHaveBeenCalled();
  });

  it('switches the open thread server-side and the list reflects it', async () => {
    activeThreadId.value = 'a';
    setThreadEffort.mockResolvedValue(thread({ id: 'a', effort: 'xhigh' }));
    // refreshThreads() re-reads from the server after the switch — return the
    // updated thread so the post-refresh list carries the new effort.
    listThreads.mockResolvedValue([thread({ id: 'a', effort: 'xhigh' })]);
    await setActiveThreadEffort('xhigh');
    expect(setThreadEffort).toHaveBeenCalledWith('a', 'xhigh');
    expect(threads.value.find((t) => t.id === 'a')?.effort).toBe('xhigh');
  });
});
