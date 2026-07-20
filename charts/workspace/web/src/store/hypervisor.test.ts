import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { HypervisorThread } from '../api/hypervisor';

// Mock the API + router collaborators so the store's rename action is exercised
// in isolation — no network, no history mutation. vi.hoisted runs before the
// mock factories so the spies exist when they capture them (TDZ dodge).
const { renameThread, listThreads, setThreadModel, createThread, getThread } = vi.hoisted(() => ({
  renameThread: vi.fn(),
  listThreads: vi.fn(),
  setThreadModel: vi.fn(),
  createThread: vi.fn(),
  getThread: vi.fn(),
}));

vi.mock('../api/hypervisor', () => ({
  renameThread: (...a: unknown[]) => renameThread(...a),
  listThreads: (...a: unknown[]) => listThreads(...a),
  setThreadModel: (...a: unknown[]) => setThreadModel(...a),
  createThread: (...a: unknown[]) => createThread(...a),
  getThread: (...a: unknown[]) => getThread(...a),
  // Unused-by-these-tests exports the store imports at module load.
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
  renameThreadTitle,
  config,
  activeThreadId,
  selectedAssistant,
  selectedModel,
  selectedWorkdir,
  assistantModels,
  setSelectedAssistant,
  setActiveThreadModel,
  sameTranscript,
  sendMessage,
  closeThread,
} from './hypervisor';
import type { HypervisorConfig } from '../api/hypervisor';
import type { HvEvent } from '../routes/hypervisor/transcript';

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

function cfg(over: Partial<HypervisorConfig> = {}): HypervisorConfig {
  return {
    enabled: true,
    defaultAssistant: 'claude',
    workdir: '/home/dev',
    readOnly: false,
    assistants: [
      { id: 'claude', label: 'Claude Code', models: ['default', 'opus', 'sonnet'] },
      { id: 'ante', label: 'Ante CLI', models: [] },
    ],
    ...over,
  };
}

beforeEach(() => {
  renameThread.mockReset();
  listThreads.mockReset();
  setThreadModel.mockReset();
  createThread.mockReset();
  getThread.mockReset();
  threads.value = [thread({ id: 'a', title: 'old' }), thread({ id: 'b', title: 'other' })];
  config.value = cfg();
  activeThreadId.value = null;
  selectedAssistant.value = 'claude';
  selectedModel.value = 'default';
  selectedWorkdir.value = '';
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

describe('model switcher (#308)', () => {
  it('assistantModels reads the list from config, default first', () => {
    expect(assistantModels('claude')).toEqual(['default', 'opus', 'sonnet']);
    expect(assistantModels('ante')).toEqual([]);
    expect(assistantModels(null)).toEqual([]);
  });

  it('setSelectedAssistant resets the model to the assistant default', () => {
    selectedModel.value = 'opus';
    setSelectedAssistant('ante'); // no models → clears the selection
    expect(selectedAssistant.value).toBe('ante');
    expect(selectedModel.value).toBe('');
    setSelectedAssistant('claude'); // back to a model-bearing assistant
    expect(selectedModel.value).toBe('default');
  });

  it('setActiveThreadModel updates only the new-chat default when no thread is open', async () => {
    activeThreadId.value = null;
    await setActiveThreadModel('sonnet');
    expect(selectedModel.value).toBe('sonnet');
    expect(setThreadModel).not.toHaveBeenCalled();
  });

  it('setActiveThreadModel optimistically patches the open thread and calls the API', async () => {
    activeThreadId.value = 'a';
    setThreadModel.mockResolvedValue(thread({ id: 'a', model: 'opus' }));
    listThreads.mockResolvedValue([thread({ id: 'a', model: 'opus' }), thread({ id: 'b' })]);
    await setActiveThreadModel('opus');
    expect(setThreadModel).toHaveBeenCalledWith('a', 'opus');
    expect(threads.value.find((t) => t.id === 'a')?.model).toBe('opus');
  });

  it('setActiveThreadModel rolls back the list when the API rejects', async () => {
    activeThreadId.value = 'a';
    threads.value = [thread({ id: 'a', model: 'default' })];
    setThreadModel.mockRejectedValue(new Error('boom'));
    await setActiveThreadModel('opus');
    expect(threads.value.find((t) => t.id === 'a')?.model).toBe('default');
  });
});

describe('sameTranscript (#348)', () => {
  function evt(over: Partial<HvEvent> = {}): HvEvent {
    return { seq: 1, ts: 1, role: 'user', type: 'message', text: 'hi', ...over };
  }

  it('treats a re-fetched but unchanged transcript as the same', () => {
    const prev = [evt(), evt({ seq: 2, role: 'assistant', text: 'hello' })];
    // A poll returns fresh object identities for identical content.
    const next = prev.map((e) => ({ ...e }));
    expect(sameTranscript(prev, next, 'session_log', 'session_log')).toBe(true);
  });

  it('treats two empty transcripts as the same', () => {
    expect(sameTranscript([], [], null, null)).toBe(true);
  });

  it('detects an appended event', () => {
    const prev = [evt()];
    const next = [evt(), evt({ seq: 2, role: 'assistant', text: 'hello' })];
    expect(sameTranscript(prev, next, 'capture', 'capture')).toBe(false);
  });

  it('detects the server replacing an optimistic (negative-seq) tail', () => {
    // sendMessage appends the user turn with a negative seq; the next poll
    // returns the server-recorded event — same length, different tail.
    const prev = [evt(), evt({ seq: -1, text: 'sent' })];
    const next = [evt(), evt({ seq: 2, text: 'sent' })];
    expect(sameTranscript(prev, next, 'capture', 'capture')).toBe(false);
  });

  it('detects a changed last-event text at the same seq', () => {
    const prev = [evt({ role: 'assistant', text: 'partial' })];
    const next = [evt({ role: 'assistant', text: 'partial + more' })];
    expect(sameTranscript(prev, next, 'capture', 'capture')).toBe(false);
  });

  it('always counts a source flip as changed (seqs are re-stamped)', () => {
    const prev = [evt()];
    const next = [evt()];
    expect(sameTranscript(prev, next, 'capture', 'session_log')).toBe(false);
  });
});

describe('workdir picker (#345)', () => {
  function stubNewThread() {
    createThread.mockResolvedValue(thread({ id: 'new' }));
    listThreads.mockResolvedValue([thread({ id: 'new' })]);
    getThread.mockResolvedValue({ thread: thread({ id: 'new' }), events: [] });
  }

  it('sendMessage forwards the selected workdir when creating a thread', async () => {
    stubNewThread();
    selectedWorkdir.value = '/home/dev/myrepo';
    await sendMessage('hello');
    expect(createThread).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'hello', workdir: '/home/dev/myrepo' }),
    );
    closeThread(); // stop the transcript poll the new thread started
  });

  it('sendMessage omits workdir when unset so the server default applies', async () => {
    stubNewThread();
    await sendMessage('hello');
    expect(createThread).toHaveBeenCalledWith(
      expect.objectContaining({ workdir: undefined }),
    );
    closeThread();
  });
});
