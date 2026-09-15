import { describe, expect, it, beforeEach, vi } from 'vitest';

/**
 * Binding an ORDINARY chat to a project (#358).
 *
 * Before this, `project_id` only ever reached the server from the AI CTO page —
 * the Chat tab (the surface most people use) had no way to say which project a
 * conversation belonged to, so nothing could group by it and its turns never
 * exported KC_PROJECT_ID. These pin the two paths: filed at creation, and
 * re-filed on an already-open chat.
 */

const { createThread, listThreads, getThread, setThreadProject } = vi.hoisted(() => ({
  createThread: vi.fn(),
  listThreads: vi.fn(),
  getThread: vi.fn(),
  setThreadProject: vi.fn(),
}));

vi.mock('../api/hypervisor', () => ({
  createThread: (...a: unknown[]) => createThread(...a),
  listThreads: (...a: unknown[]) => listThreads(...a),
  getThread: (...a: unknown[]) => getThread(...a),
  setThreadProject: (...a: unknown[]) => setThreadProject(...a),
  getHypervisorConfig: vi.fn(),
  renameThread: vi.fn(),
  sendThreadMessage: vi.fn(),
  stopThread: vi.fn(),
  deleteThread: vi.fn(),
  listDeletedThreads: vi.fn(),
  restoreThread: vi.fn(),
  setThreadModel: vi.fn(),
  setThreadEffort: vi.fn(),
}));
vi.mock('../api/tasks', () => ({ listTasks: vi.fn() }));
vi.mock('./router', () => ({
  navigate: vi.fn(),
  currentPath: { value: '/hypervisor' },
}));

import {
  threads,
  activeThreadId,
  chatError,
  selectedProject,
  selectedAssistant,
  selectedModel,
  selectedEffort,
  newChatMode,
  setActiveThreadProject,
  sendMessage,
} from './hypervisor';
import { claudeReady } from './claude';

beforeEach(() => {
  vi.clearAllMocks();
  threads.value = [];
  activeThreadId.value = null;
  chatError.value = null;
  newChatMode.value = '';
  selectedProject.value = '';
  selectedAssistant.value = 'claude';
  selectedModel.value = '';
  selectedEffort.value = '';
  claudeReady.value = true;
  listThreads.mockResolvedValue([]);
  getThread.mockResolvedValue({ thread: { status: 'idle' }, events: [] });
  createThread.mockResolvedValue({ id: 't1' });
  setThreadProject.mockResolvedValue({});
});

describe('a new chat is created filed into the picked project', () => {
  it('sends project_id for a plain chat', async () => {
    selectedProject.value = 'kc';
    await sendMessage('hello');
    expect(createThread.mock.calls[0][0]).toMatchObject({ project_id: 'kc' });
    // Still a plain chat — filing it into a project must not make it a CTO one.
    expect(createThread.mock.calls[0][0].persona).toBeUndefined();
  });

  it('omits project_id entirely when no project is picked', async () => {
    await sendMessage('hello');
    expect(createThread.mock.calls[0][0].project_id).toBeUndefined();
  });
});

describe('setActiveThreadProject', () => {
  it('moves only the new-chat default when no chat is open', async () => {
    await setActiveThreadProject('kc');
    expect(selectedProject.value).toBe('kc');
    expect(setThreadProject).not.toHaveBeenCalled();
  });

  it('moves it for a CTO-mode chat too — one surface, one picker (#683)', async () => {
    // The AI CTO page used to hold a competing project selection that this
    // picker was forbidden to touch. With one surface there is no other
    // selection to protect.
    newChatMode.value = 'cto';
    await setActiveThreadProject('kc');
    expect(selectedProject.value).toBe('kc');
    expect(setThreadProject).not.toHaveBeenCalled();
  });

  it('re-files an open chat server-side and patches the list at once', async () => {
    activeThreadId.value = 't1';
    threads.value = [
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { id: 't1', title: 'c', assistant: 'claude', project_id: '' } as any,
    ];
    await setActiveThreadProject('kc');
    expect(setThreadProject).toHaveBeenCalledWith('t1', 'kc');
    // Optimistic patch happens before the refresh, which the mock resolves to [].
    expect(listThreads).toHaveBeenCalled();
  });

  it('clears the binding with an empty id', async () => {
    activeThreadId.value = 't1';
    threads.value = [
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { id: 't1', title: 'c', assistant: 'claude', project_id: 'kc' } as any,
    ];
    await setActiveThreadProject('');
    expect(setThreadProject).toHaveBeenCalledWith('t1', '');
  });

  it('rolls back and surfaces the error when the server refuses', async () => {
    // The server rejects re-filing a CTO chat (its brief is baked in at
    // creation), so the optimistic patch must not stick.
    activeThreadId.value = 't1';
    threads.value = [
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { id: 't1', title: 'c', assistant: 'claude', project_id: '' } as any,
    ];
    setThreadProject.mockRejectedValue(new Error("a CTO chat's project is fixed"));
    await setActiveThreadProject('kc');
    expect(threads.value[0].project_id).toBe('');
    expect(chatError.value).toContain('CTO chat');
  });
});
