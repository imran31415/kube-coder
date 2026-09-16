import { describe, expect, it, beforeEach, vi } from 'vitest';

/**
 * A listed-but-not-ready agent in Chat (#702) — e.g. DeepSeek Harness with no
 * API key. It must never be pre-selected, a new chat on it must be refused
 * with the reason (not sent to fail on turn 1), and saving a key must flip it
 * without a reload.
 */

const { createThread, listThreads, getThread, getHypervisorConfig } = vi.hoisted(() => ({
  createThread: vi.fn(),
  listThreads: vi.fn(),
  getThread: vi.fn(),
  getHypervisorConfig: vi.fn(),
}));

vi.mock('../api/hypervisor', () => ({
  createThread: (...a: unknown[]) => createThread(...a),
  listThreads: (...a: unknown[]) => listThreads(...a),
  getThread: (...a: unknown[]) => getThread(...a),
  getHypervisorConfig: (...a: unknown[]) => getHypervisorConfig(...a),
  setThreadProject: vi.fn(),
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
  config,
  activeThreadId,
  chatError,
  selectedAssistant,
  selectedModel,
  selectedEffort,
  newChatMode,
  sendMessage,
  initHypervisor,
  refreshHypervisorConfig,
  resolveProjectDefaults,
  newChatBlockedReason,
} from './hypervisor';
import { claudeReady } from './claude';
import type { HypervisorConfig } from '../api/hypervisor';

const REASON =
  'DeepSeek Harness needs a DeepSeek API key. Add it in Settings → Provider API keys.';

function cfg(dshReady: boolean, defaultAssistant = 'claude'): HypervisorConfig {
  return {
    enabled: true,
    defaultAssistant,
    assistants: [
      { id: 'claude', label: 'Claude Code', default: true, ready: true },
      dshReady
        ? { id: 'deepseek-harness', label: 'DeepSeek Harness', ready: true }
        : {
            id: 'deepseek-harness',
            label: 'DeepSeek Harness',
            ready: false,
            needs: ['DEEPSEEK_API_KEY'],
            notReadyReason: REASON,
          },
    ],
  } as HypervisorConfig;
}

beforeEach(() => {
  vi.clearAllMocks();
  config.value = cfg(false);
  activeThreadId.value = null;
  chatError.value = null;
  newChatMode.value = '';
  selectedAssistant.value = 'claude';
  selectedModel.value = '';
  selectedEffort.value = '';
  claudeReady.value = true;
  listThreads.mockResolvedValue([]);
  getThread.mockResolvedValue({ thread: { status: 'idle' }, events: [] });
  createThread.mockResolvedValue({ id: 't1' });
});

describe('a new chat on a not-ready agent', () => {
  it('is refused with the reason and never creates a thread', async () => {
    selectedAssistant.value = 'deepseek-harness';
    expect(newChatBlockedReason()).toBe(REASON);
    await sendMessage('hello');
    expect(createThread).not.toHaveBeenCalled();
    expect(chatError.value).toBe(REASON);
  });

  it('goes through once the agent is ready', async () => {
    config.value = cfg(true);
    selectedAssistant.value = 'deepseek-harness';
    expect(newChatBlockedReason()).toBeNull();
    await sendMessage('hello');
    expect(createThread).toHaveBeenCalledTimes(1);
    expect(createThread.mock.calls[0][0]).toMatchObject({ assistant: 'deepseek-harness' });
  });

  it('does not block an already-open chat (the server decides there)', () => {
    selectedAssistant.value = 'deepseek-harness';
    activeThreadId.value = 't-open';
    expect(newChatBlockedReason()).toBeNull();
  });

  it('does not block a ready agent', () => {
    expect(newChatBlockedReason()).toBeNull();
  });
});

describe('defaults never land on a not-ready agent', () => {
  it('initHypervisor skips a not-ready server default', async () => {
    selectedAssistant.value = '';
    getHypervisorConfig.mockResolvedValueOnce(cfg(false, 'deepseek-harness'));
    await initHypervisor();
    expect(selectedAssistant.value).toBe('claude');
  });

  it('a project default that is not ready falls back to a ready one', () => {
    expect(resolveProjectDefaults({ default_assistant: 'deepseek-harness' }).assistant).toBe(
      'claude',
    );
    config.value = cfg(true);
    expect(resolveProjectDefaults({ default_assistant: 'deepseek-harness' }).assistant).toBe(
      'deepseek-harness',
    );
  });
});

describe('refreshHypervisorConfig', () => {
  it('flips a not-ready agent to ready after a key is saved', async () => {
    selectedAssistant.value = 'deepseek-harness';
    expect(newChatBlockedReason()).toBe(REASON);
    getHypervisorConfig.mockResolvedValueOnce(cfg(true));
    await refreshHypervisorConfig();
    expect(newChatBlockedReason()).toBeNull();
  });

  it('keeps the last-good config when the fetch fails', async () => {
    getHypervisorConfig.mockRejectedValueOnce(new Error('offline'));
    const before = config.value;
    await refreshHypervisorConfig();
    expect(config.value).toBe(before);
  });
});
