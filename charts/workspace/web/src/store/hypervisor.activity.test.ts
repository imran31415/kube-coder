import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { HypervisorThread } from '../api/hypervisor';
import { partitionThreads } from '../routes/hypervisor/chatTabs';

const api = vi.hoisted(() => ({
  getThread: vi.fn(), sendThreadMessage: vi.fn(), listThreads: vi.fn(),
  deleteThread: vi.fn(), renameThread: vi.fn(), stopThread: vi.fn(),
}));
vi.mock('../api/hypervisor', async (original) => ({
  ...await original<typeof import('../api/hypervisor')>(), ...api,
}));

import {
  threads, openThread, closeThread, sendMessage, activeStatus, activeThreadId,
  refreshThreads, removeThread, renameThreadTitle, stopMessage, chatError,
} from './hypervisor';

const NOW = 1_800_000_000_000;
const old: HypervisorThread = {
  id: 'old', title: 'Old CTO', assistant: 'codex', persona: 'cto', status: 'idle',
  created_at: (NOW - 172800000) / 1000, updated_at: (NOW - 172800000) / 1000,
};
const other: HypervisorThread = { ...old, id: 'other', persona: '', updated_at: NOW / 1000 };
const running: HypervisorThread = { ...old, status: 'running', updated_at: NOW / 1000 };
let serverThreads: HypervisorThread[];

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  serverThreads = [old, other];
  threads.value = [...serverThreads];
  api.listThreads.mockImplementation(async () => [...serverThreads]);
  api.getThread.mockImplementation(async (id: string) => ({
    thread: serverThreads.find(t => t.id === id), events: [], source: 'capture',
  }));
  api.sendThreadMessage.mockImplementation(async (id: string) => {
    serverThreads = serverThreads.map(t => t.id === id ? { ...t, status: 'running', updated_at: NOW / 1000 } : t);
  });
});

afterEach(() => { closeThread(); vi.useRealTimers(); vi.resetAllMocks(); });

it.each(['cto', ''])('keeps a resumed %s chat in Active after switching chats (#750)', async (persona) => {
  serverThreads = [{ ...old, persona }, other];
  threads.value = [...serverThreads];
  await openThread('old');
  await sendMessage('Continue working');
  expect(activeStatus.value).toBe('running');
  await openThread('other');
  expect(partitionThreads(threads.value, activeThreadId.value, NOW).active.map(t => t.id)).toContain('old');
});

it('merges live details, orders by activity, and avoids identical list updates', async () => {
  threads.value = [other, old];
  serverThreads = [running, other];
  await openThread('old');
  expect(threads.value.find(t => t.id === 'old')).toEqual(running);
  const previous = threads.value;
  await vi.advanceTimersByTimeAsync(2000);
  expect(threads.value).toBe(previous);
});

it('keeps completed and stopped chats Active using the server timestamp', async () => {
  serverThreads = [running, other];
  await openThread('old');
  serverThreads = [{ ...running, status: 'idle' }, other];
  await vi.advanceTimersByTimeAsync(2000);
  expect(activeStatus.value).toBe('idle');
  expect(partitionThreads(threads.value, null, NOW).active.map(t => t.id)).toContain('old');
  await sendMessage('Continue');
  api.stopThread.mockImplementation(async () => { serverThreads = [{ ...running, status: 'idle' }, other]; });
  await stopMessage();
  expect(threads.value.find(t => t.id === 'old')?.status).toBe('idle');
});

it('shares concurrent list requests', async () => {
  const pending = deferred<HypervisorThread[]>();
  api.listThreads.mockReturnValueOnce(pending.promise);
  const first = refreshThreads();
  const second = refreshThreads();
  expect(api.listThreads).toHaveBeenCalledTimes(1);
  pending.resolve([running, other]);
  await Promise.all([first, second]);
  expect(threads.value[0]).toEqual(running);
});

it('retries a list snapshot overtaken by fresh conversation details', async () => {
  const pending = deferred<HypervisorThread[]>();
  api.listThreads.mockReturnValueOnce(pending.promise);
  const refresh = refreshThreads();
  serverThreads = [running, other];
  await openThread('old');
  pending.resolve([old, other]);
  await refresh;
  expect(api.listThreads).toHaveBeenCalledTimes(2);
  expect(threads.value[0]).toEqual(running);
});

it('does not let a pre-send list response undo resumed activity', async () => {
  await openThread('old');
  const pending = deferred<HypervisorThread[]>();
  api.listThreads.mockReturnValueOnce(pending.promise);
  const refresh = refreshThreads();
  const send = sendMessage('Continue');
  await vi.advanceTimersByTimeAsync(0);
  pending.resolve([old, other]);
  await Promise.all([refresh, send]);
  expect(threads.value.find(t => t.id === 'old')?.status).toBe('running');
});

it('does not resurrect a deleted chat from a delayed list or detail response', async () => {
  const detail = deferred<{ thread: HypervisorThread; events: [] }>();
  const list = deferred<HypervisorThread[]>();
  api.getThread.mockReturnValueOnce(detail.promise);
  api.listThreads.mockReturnValueOnce(list.promise);
  const open = openThread('old');
  const refresh = refreshThreads();
  api.deleteThread.mockImplementation(async () => { serverThreads = [other]; });
  const remove = removeThread('old');
  await vi.advanceTimersByTimeAsync(0);
  list.resolve([old, other]);
  await Promise.all([refresh, remove]);
  detail.resolve({ thread: old, events: [] });
  await open;
  expect(threads.value).toEqual([other]);
  expect(activeThreadId.value).toBeNull();
  const calls = api.getThread.mock.calls.length;
  await vi.advanceTimersByTimeAsync(4000);
  expect(api.getThread).toHaveBeenCalledTimes(calls);
});

it('protects an optimistic rename while its write is pending', async () => {
  const write = deferred<HypervisorThread>();
  api.renameThread.mockReturnValueOnce(write.promise);
  const rename = renameThreadTitle('old', 'Renamed CTO');
  await refreshThreads();
  expect(threads.value.find(t => t.id === 'old')?.title).toBe('Renamed CTO');
  serverThreads = [{ ...old, title: 'Renamed CTO' }, other];
  write.resolve(serverThreads[0]);
  await rename;
  expect(threads.value.find(t => t.id === 'old')?.title).toBe('Renamed CTO');
});

it('ignores details from a previously selected chat', async () => {
  const detail = deferred<{ thread: HypervisorThread; events: [] }>();
  api.getThread.mockReturnValueOnce(detail.promise);
  const open = openThread('old');
  await openThread('other');
  detail.resolve({ thread: running, events: [] });
  await open;
  expect(activeThreadId.value).toBe('other');
  expect(activeStatus.value).toBe('idle');
});

it('refreshes the original send target without replacing the newly selected chat', async () => {
  await openThread('old');
  const sent = deferred<void>();
  api.sendThreadMessage.mockReturnValueOnce(sent.promise);
  const send = sendMessage('Continue');
  await openThread('other');
  serverThreads = [running, other];
  sent.resolve();
  await send;
  expect(api.sendThreadMessage).toHaveBeenCalledWith('old', 'Continue');
  expect(activeThreadId.value).toBe('other');
  expect(threads.value.find(t => t.id === 'old')?.status).toBe('running');
});

it('retains the last-good list and optionally reports polling failures', async () => {
  const previous = threads.value;
  api.listThreads.mockRejectedValue(new Error('Offline'));
  await refreshThreads();
  expect(threads.value).toBe(previous);
  await expect(refreshThreads({ rethrow: true })).rejects.toThrow('Offline');
  expect(threads.value).toBe(previous);
  api.listThreads.mockResolvedValue([running, other]);
  await refreshThreads();
  expect(threads.value[0]).toEqual(running);
});

it('does not report a successful send as failed when the list refresh fails', async () => {
  await openThread('old');
  api.listThreads.mockRejectedValue(new Error('Offline'));
  await sendMessage('Continue');
  expect(chatError.value).toBeNull();
  expect(threads.value.find(t => t.id === 'old')?.status).toBe('running');
});

it('restores server status after a rejected send without inventing activity', async () => {
  await openThread('old');
  api.sendThreadMessage.mockRejectedValue(new Error('Rejected'));
  await sendMessage('Continue');
  expect(chatError.value).toBe('Rejected');
  expect(activeStatus.value).toBe('idle');
  expect(partitionThreads(threads.value, null, NOW).past.map(t => t.id)).toContain('old');
});
