import { describe, expect, it, beforeEach, vi } from 'vitest';

/**
 * CTO scoping of the assistant/model/effort selection (#483) plus the effort
 * switch itself (#362).
 *
 * The bug this pins shut: the CTO page reuses this store, so before #483 it
 * sent whatever the CHAT TAB had selected. A model picked for a throwaway chat
 * silently became the CTO's. The two surfaces now hold separate signals and
 * sendMessage reads the one belonging to the surface it is on.
 */

const { createThread, listThreads, getThread, setThreadEffort, setThreadModel } =
  vi.hoisted(() => ({
    createThread: vi.fn(),
    listThreads: vi.fn(),
    getThread: vi.fn(),
    setThreadEffort: vi.fn(),
    setThreadModel: vi.fn(),
  }));

vi.mock('../api/hypervisor', () => ({
  createThread: (...a: unknown[]) => createThread(...a),
  listThreads: (...a: unknown[]) => listThreads(...a),
  getThread: (...a: unknown[]) => getThread(...a),
  setThreadEffort: (...a: unknown[]) => setThreadEffort(...a),
  setThreadModel: (...a: unknown[]) => setThreadModel(...a),
  getHypervisorConfig: vi.fn(),
  renameThread: vi.fn(),
  sendThreadMessage: vi.fn(),
  stopThread: vi.fn(),
  deleteThread: vi.fn(),
  listDeletedThreads: vi.fn(),
  restoreThread: vi.fn(),
}));
vi.mock('../api/tasks', () => ({ listTasks: vi.fn() }));
vi.mock('./router', () => ({
  navigate: vi.fn(),
  currentPath: { value: '/cto' },
}));

import {
  config,
  threads,
  activeThreadId,
  chatPersona,
  selectedAssistant,
  selectedModel,
  selectedEffort,
  ctoAssistant,
  ctoModel,
  ctoEffort,
  assistantEfforts,
  assistantEffortDefault,
  seedCtoConfig,
  setCtoAssistant,
  setSelectedAssistant,
  setActiveThreadEffort,
  setChatContext,
  sendMessage,
} from './hypervisor';
import { claudeReady } from './claude';

// Mirrors the server's /api/hypervisor/config shape: the effort axis and the
// default level are per-assistant (#362), so a knob-less assistant simply has
// neither and every selector keyed off them stays hidden.
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
    {
      id: 'codex',
      label: 'Codex',
      models: [],
      efforts: ['low', 'medium', 'high', 'xhigh', 'max'] as const,
      effort: 'high',
      effortCap: 'xhigh',
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  config.value = CONFIG as any;
  threads.value = [];
  activeThreadId.value = null;
  chatPersona.value = '';
  selectedAssistant.value = 'claude';
  selectedModel.value = 'default';
  selectedEffort.value = 'high';
  ctoAssistant.value = '';
  ctoModel.value = '';
  ctoEffort.value = '';
  claudeReady.value = true;
  listThreads.mockResolvedValue([]);
  getThread.mockResolvedValue({ thread: { status: 'idle' }, events: [] });
  createThread.mockResolvedValue({ id: 't1' });
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

describe('seeding the CTO selection from a project (#483)', () => {
  it('uses the project defaults when it has them', () => {
    seedCtoConfig({
      default_assistant: 'claude',
      default_model: 'opus',
      default_effort: 'xhigh',
    });
    expect(ctoAssistant.value).toBe('claude');
    expect(ctoModel.value).toBe('opus');
    expect(ctoEffort.value).toBe('xhigh');
  });

  it('falls back to the workspace default for an unconfigured project', () => {
    seedCtoConfig(null);
    expect(ctoAssistant.value).toBe('claude');
    expect(ctoModel.value).toBe('default');
    expect(ctoEffort.value).toBe('high');
  });

  it('ignores a stored value the assistant no longer offers', () => {
    seedCtoConfig({ default_assistant: 'claude', default_model: 'retired-model' });
    expect(ctoModel.value).toBe('default');
  });

  it('ignores an off-axis stored effort', () => {
    seedCtoConfig({ default_assistant: 'claude', default_effort: 'ludicrous' });
    expect(ctoEffort.value).toBe('high');
  });

  it('ignores a provider this workspace can no longer offer', () => {
    // A project pinned to an assistant whose key/binary is gone must not leave
    // the picker showing a dead option — it falls back to the workspace default.
    seedCtoConfig({ default_assistant: 'opencode-zen', default_model: 'x' });
    expect(ctoAssistant.value).toBe('claude');
    expect(ctoModel.value).toBe('default');
  });

  it('leaves the effort empty for an assistant with no knob', () => {
    seedCtoConfig({ default_assistant: 'ante', default_effort: 'max' });
    expect(ctoEffort.value).toBe('');
  });

  it('never touches the Chat tab selection', () => {
    selectedAssistant.value = 'ante';
    selectedModel.value = '';
    seedCtoConfig({ default_assistant: 'claude', default_model: 'opus' });
    expect(selectedAssistant.value).toBe('ante');
    expect(selectedModel.value).toBe('');
  });
});

describe('picking an assistant resets its dependent dials', () => {
  it('does so for the CTO surface', () => {
    setCtoAssistant('claude');
    expect(ctoModel.value).toBe('default');
    expect(ctoEffort.value).toBe('high');
    setCtoAssistant('ante');
    expect(ctoModel.value).toBe('');
    expect(ctoEffort.value).toBe('');
  });

  it('and for the Chat tab', () => {
    setSelectedAssistant('ante');
    expect(selectedModel.value).toBe('');
    expect(selectedEffort.value).toBe('');
  });
});

describe('sendMessage sends the current surface selection', () => {
  it('the CTO uses its own, not the Chat tab’s', async () => {
    setChatContext('cto', 'kube-coder');
    seedCtoConfig({
      default_assistant: 'claude',
      default_model: 'opus',
      default_effort: 'xhigh',
    });
    selectedAssistant.value = 'ante';
    selectedModel.value = '';
    selectedEffort.value = '';

    await sendMessage('ship it');

    expect(createThread).toHaveBeenCalledTimes(1);
    expect(createThread.mock.calls[0][0]).toMatchObject({
      assistant: 'claude',
      model: 'opus',
      effort: 'xhigh',
      persona: 'cto',
      project_id: 'kube-coder',
    });
  });

  it('the Chat tab keeps using its own', async () => {
    setChatContext('', null);
    ctoAssistant.value = 'claude';
    ctoModel.value = 'opus';
    ctoEffort.value = 'max';
    selectedAssistant.value = 'ante';
    selectedModel.value = '';
    selectedEffort.value = '';

    await sendMessage('hello');

    expect(createThread.mock.calls[0][0]).toMatchObject({ assistant: 'ante' });
    expect(createThread.mock.calls[0][0].model).toBeUndefined();
    expect(createThread.mock.calls[0][0].effort).toBeUndefined();
  });
});

describe('setActiveThreadEffort is surface-scoped too (#483)', () => {
  it('moves the surface default when no thread is open', async () => {
    setChatContext('cto', 'kc');
    await setActiveThreadEffort('low');
    expect(ctoEffort.value).toBe('low');
    expect(selectedEffort.value).toBe('high');
    expect(setThreadEffort).not.toHaveBeenCalled();

    setChatContext('', null);
    await setActiveThreadEffort('max');
    expect(selectedEffort.value).toBe('max');
  });

  it('still switches an OPEN thread server-side on either surface', async () => {
    // The open-thread path is shared with the Chat tab (covered in
    // hypervisor.effort.test.ts); pinned here so the CTO branch above can never
    // swallow a real switch.
    setChatContext('cto', 'kc');
    activeThreadId.value = 't1';
    threads.value = [
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { id: 't1', title: 'c', assistant: 'claude', effort: 'high' } as any,
    ];
    setThreadEffort.mockResolvedValue({});

    await setActiveThreadEffort('xhigh');

    expect(setThreadEffort).toHaveBeenCalledWith('t1', 'xhigh');
    expect(ctoEffort.value).toBe('');
  });
});
