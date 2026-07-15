import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { HypervisorThread } from '../api/hypervisor';

// Mock the API + router collaborators so the store's rename action is exercised
// in isolation — no network, no history mutation. vi.hoisted runs before the
// mock factories so the spies exist when they capture them (TDZ dodge).
const { renameThread, listThreads } = vi.hoisted(() => ({
  renameThread: vi.fn(),
  listThreads: vi.fn(),
}));

vi.mock('../api/hypervisor', () => ({
  renameThread: (...a: unknown[]) => renameThread(...a),
  listThreads: (...a: unknown[]) => listThreads(...a),
  // Unused-by-these-tests exports the store imports at module load.
  getHypervisorConfig: vi.fn(),
  createThread: vi.fn(),
  getThread: vi.fn(),
  sendThreadMessage: vi.fn(),
  stopThread: vi.fn(),
  deleteThread: vi.fn(),
}));
vi.mock('../api/tasks', () => ({ listTasks: vi.fn() }));
vi.mock('./router', () => ({
  navigate: vi.fn(),
  currentPath: { value: '/hypervisor' },
}));

import { threads, renameThreadTitle } from './hypervisor';

function thread(over: Partial<HypervisorThread> = {}): HypervisorThread {
  return {
    id: 'a',
    title: 'old',
    assistant: 'claude',
    status: 'idle',
    created_at: 1,
    updated_at: 1,
    ...over,
  };
}

beforeEach(() => {
  renameThread.mockReset();
  listThreads.mockReset();
  threads.value = [thread({ id: 'a', title: 'old' }), thread({ id: 'b', title: 'other' })];
});

describe('renameThreadTitle', () => {
  it('optimistically patches the matching thread, trimming whitespace', async () => {
    renameThread.mockResolvedValue(thread({ id: 'a', title: 'renamed' }));
    listThreads.mockResolvedValue([
      thread({ id: 'a', title: 'renamed' }),
      thread({ id: 'b', title: 'other' }),
    ]);
    await renameThreadTitle('a', '  renamed  ');
    expect(renameThread).toHaveBeenCalledWith('a', 'renamed');
    expect(threads.value.find((t) => t.id === 'a')?.title).toBe('renamed');
    // Untouched threads keep their title.
    expect(threads.value.find((t) => t.id === 'b')?.title).toBe('other');
  });

  it('is a no-op for a blank title', async () => {
    await renameThreadTitle('a', '   ');
    expect(renameThread).not.toHaveBeenCalled();
    expect(threads.value.find((t) => t.id === 'a')?.title).toBe('old');
  });

  it('rolls back to the previous list when the API rejects', async () => {
    renameThread.mockRejectedValue(new Error('boom'));
    await renameThreadTitle('a', 'renamed');
    expect(threads.value.find((t) => t.id === 'a')?.title).toBe('old');
  });
});
