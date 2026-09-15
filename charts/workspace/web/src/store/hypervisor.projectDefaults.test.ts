import { describe, expect, it, beforeEach, vi } from 'vitest';

/**
 * Per-project assistant defaults (#483) and the effort axis (#362).
 *
 * This file used to pin a split that no longer exists: the AI CTO page reused
 * this store, so it carried its OWN assistant/model/effort signals to stop a
 * model picked for a throwaway chat silently becoming the CTO's. With one chat
 * surface (#683) there is no second selection to protect — what survives is the
 * part that was always about the PROJECT: resolving a project's stored defaults
 * into something this workspace can actually offer, and seeding the pickers
 * from it.
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
  assistantEffortDefault,
  assistantEfforts,
  config,
  resolveProjectDefaults,
  seedChatConfig,
  selectedAssistant,
  selectedEffort,
  selectedModel,
  setSelectedAssistant,
} from './hypervisor';

/** Mirrors the server's /api/hypervisor/config shape: the effort axis and the
 *  default level are per-assistant (#362), so a knob-less assistant has neither
 *  and every selector keyed off them stays hidden. */
const CONFIG = {
  enabled: true,
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
  assistants: [
    {
      id: 'claude',
      label: 'Claude Code',
      models: ['default', 'opus'],
      efforts: ['low', 'medium', 'high', 'xhigh', 'max'] as const,
      effort: 'high',
      effortCap: 'xhigh',
    },
    { id: 'ante', label: 'Ante CLI', models: [], efforts: [] as const },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  config.value = CONFIG as any;
  selectedAssistant.value = 'claude';
  selectedModel.value = 'default';
  selectedEffort.value = 'high';
});

describe('per-assistant effort availability', () => {
  it('reads the levels the server declared', () => {
    expect(assistantEfforts('claude')).toHaveLength(5);
    expect(assistantEfforts('ante')).toEqual([]);
    expect(assistantEfforts(null)).toEqual([]);
  });

  it('defaults to the assistant default, and to nothing without support', () => {
    expect(assistantEffortDefault('claude')).toBe('high');
    expect(assistantEffortDefault('ante')).toBe('');
  });
});

describe('resolving a project’s stored defaults (#483)', () => {
  it('uses the project defaults when it has them', () => {
    expect(
      resolveProjectDefaults({
        default_assistant: 'claude',
        default_model: 'opus',
        default_effort: 'xhigh',
      }),
    ).toEqual({ assistant: 'claude', model: 'opus', effort: 'xhigh' });
  });

  it('falls back to the workspace default for an unconfigured project', () => {
    // The state every project starts in — nothing is written until the user
    // explicitly sets a default.
    expect(resolveProjectDefaults(null)).toEqual({
      assistant: 'claude',
      model: 'default',
      effort: 'high',
    });
  });

  it('ignores a stored model the assistant no longer offers', () => {
    expect(
      resolveProjectDefaults({ default_assistant: 'claude', default_model: 'retired' }).model,
    ).toBe('default');
  });

  it('ignores an off-axis stored effort', () => {
    expect(
      resolveProjectDefaults({ default_assistant: 'claude', default_effort: 'ludicrous' })
        .effort,
    ).toBe('high');
  });

  it('ignores a provider this workspace can no longer offer', () => {
    // A project pinned to an assistant whose key/binary is gone must not leave
    // the picker showing a dead option.
    expect(
      resolveProjectDefaults({ default_assistant: 'opencode-zen', default_model: 'x' }),
    ).toMatchObject({ assistant: 'claude', model: 'default' });
  });

  it('leaves the effort empty for an assistant with no knob', () => {
    expect(
      resolveProjectDefaults({ default_assistant: 'ante', default_effort: 'max' }).effort,
    ).toBe('');
  });
});

describe('seedChatConfig', () => {
  it('moves the pickers onto the project’s defaults', () => {
    seedChatConfig({ default_assistant: 'ante' });
    expect(selectedAssistant.value).toBe('ante');
    expect(selectedModel.value).toBe('');
    expect(selectedEffort.value).toBe('');
  });
});

describe('picking an assistant resets its dependent dials', () => {
  it('drops a model and effort the new assistant does not offer', () => {
    setSelectedAssistant('ante');
    expect(selectedModel.value).toBe('');
    expect(selectedEffort.value).toBe('');
    setSelectedAssistant('claude');
    expect(selectedModel.value).toBe('default');
    expect(selectedEffort.value).toBe('high');
  });
});
