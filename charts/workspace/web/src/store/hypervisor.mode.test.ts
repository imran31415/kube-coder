import { describe, expect, it, beforeEach, vi } from 'vitest';

/**
 * Chat's Mode picker (#683) — creation-time persona selection.
 *
 * The AI CTO was only ever a thread with a different preamble, delivered once
 * via --append-system-prompt on turn 1. Picking "CTO" in Chat therefore has to
 * mean exactly one thing: the NEXT thread is created with persona='cto'. This
 * pins that, plus the two rules that ride along with it (the Claude-credential
 * gate and the project-resolved workdir), and pins that the #483 page-scoped
 * assistant split does NOT leak into Chat's own pickers.
 */

const { createThread, listThreads, getThread } = vi.hoisted(() => ({
  createThread: vi.fn(),
  listThreads: vi.fn(),
  getThread: vi.fn(),
}));

vi.mock('../api/hypervisor', () => ({
  createThread: (...a: unknown[]) => createThread(...a),
  listThreads: (...a: unknown[]) => listThreads(...a),
  getThread: (...a: unknown[]) => getThread(...a),
  getHypervisorConfig: vi.fn(),
  renameThread: vi.fn(),
  sendThreadMessage: vi.fn(),
  stopThread: vi.fn(),
  deleteThread: vi.fn(),
  listDeletedThreads: vi.fn(),
  restoreThread: vi.fn(),
  setThreadModel: vi.fn(),
  setThreadEffort: vi.fn(),
  setThreadProject: vi.fn(),
}));
vi.mock('../api/tasks', () => ({ listTasks: vi.fn() }));
vi.mock('./router', () => ({
  navigate: vi.fn(),
  currentPath: { value: '/hypervisor' },
}));

import {
  activeThreadId,
  chatError,
  config,
  isCtoChat,
  newChatMode,
  selectedAssistant,
  selectedModel,
  selectedProject,
  selectedWorkdir,
  sendMessage,
  threads,
} from './hypervisor';
import { claudeReady } from './claude';

const CONFIG = {
  enabled: true,
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
  assistants: [{ id: 'claude', label: 'Claude Code', models: ['default', 'opus'] }],
};

beforeEach(() => {
  vi.clearAllMocks();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  config.value = CONFIG as any;
  threads.value = [];
  activeThreadId.value = null;
  chatError.value = null;
  newChatMode.value = '';
  selectedAssistant.value = 'claude';
  selectedModel.value = 'default';
  selectedProject.value = '';
  selectedWorkdir.value = '/home/dev/kube-coder';
  claudeReady.value = true;
  listThreads.mockResolvedValue([]);
  getThread.mockResolvedValue({ thread: { status: 'idle' }, events: [] });
  createThread.mockResolvedValue({ id: 't1' });
});

describe('the mode a new chat is created with', () => {
  it('is a plain chat by default — no persona goes to the server', async () => {
    await sendMessage('hello');
    expect(createThread.mock.calls[0][0].persona).toBeUndefined();
  });

  it('creates a CTO thread when the picker is on CTO', async () => {
    newChatMode.value = 'cto';
    await sendMessage('what should I focus on?');
    expect(createThread.mock.calls[0][0].persona).toBe('cto');
  });

  it('files a CTO chat into the project the user picked', async () => {
    newChatMode.value = 'cto';
    selectedProject.value = 'kc';
    await sendMessage('hi');
    expect(createThread.mock.calls[0][0]).toMatchObject({
      persona: 'cto',
      project_id: 'kc',
    });
  });
});

describe('what riding on the mode actually changes', () => {
  it('binds the project from the picker, whatever the mode', async () => {
    newChatMode.value = 'cto';
    selectedProject.value = 'pool';
    await sendMessage('plan it');
    expect(createThread.mock.calls[0][0].project_id).toBe('pool');
  });

  it('uses Chat’s own assistant selection — there is no second one', async () => {
    // The AI CTO page carried its own assistant/model/effort (#483) precisely
    // because it was a second surface. With one surface, one selection.
    newChatMode.value = 'cto';
    expect(isCtoChat()).toBe(true);
    await sendMessage('plan it');
    expect(createThread.mock.calls[0][0]).toMatchObject({
      assistant: 'claude',
      model: 'default',
    });
  });

  it('lets the server resolve the folder from the project', async () => {
    // A CTO chat starts in its project's folder; sending the picker's default
    // would defeat the server's `not workdir` guard.
    newChatMode.value = 'cto';
    await sendMessage('plan it');
    expect(createThread.mock.calls[0][0].workdir).toBeUndefined();
  });

  it('sends the picker’s folder for a plain chat', async () => {
    await sendMessage('and a plain one');
    expect(createThread.mock.calls[0][0].workdir).toBe('/home/dev/kube-coder');
  });

  it('refuses a CTO send with no Claude credential, wherever it came from', async () => {
    claudeReady.value = false;
    newChatMode.value = 'cto';
    await sendMessage('build me a site');
    expect(createThread).not.toHaveBeenCalled();
    expect(chatError.value).toContain('connect Claude in Provider settings');
  });

  it('also guides a plain Claude chat to authentication', async () => {
    claudeReady.value = false;
    await sendMessage('hello');
    expect(createThread).not.toHaveBeenCalled();
    expect(chatError.value).toContain('Authentication required:');
  });

  it.each(['codex', 'ante', 'antigravity', 'opencode-zen'])('does not gate %s CTO chats on Claude', async (assistant) => {
    claudeReady.value = false;
    newChatMode.value = 'cto';
    selectedAssistant.value = assistant;
    await sendMessage('hello');
    expect(createThread).toHaveBeenCalledWith(expect.objectContaining({ assistant, persona: 'cto' }));
  });
});

describe('the picker is a new-chat default, like Agent and Folder', () => {
  it('survives sending — the next new chat keeps the mode', async () => {
    newChatMode.value = 'cto';
    await sendMessage('one');
    expect(newChatMode.value).toBe('cto');
  });
});
