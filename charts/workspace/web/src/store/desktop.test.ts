import { describe, expect, it, beforeEach, vi } from 'vitest';

// Stub the two collaborators startBuildFromPrompt leans on: createTask
// (which would otherwise hit the network) and navigate (which mutates
// history). We assert on the call args rather than real side effects.
// vi.mock is hoisted above module-level consts, so anything its factories
// capture must be created with vi.hoisted (which runs first) to dodge the TDZ.
const {
  createTask,
  navigate,
  createThread,
  getHypervisorConfig,
  refreshThreads,
  pushToast,
  hvConfig,
} = vi.hoisted(() => ({
  createTask: vi.fn(),
  navigate: vi.fn(),
  createThread: vi.fn(),
  getHypervisorConfig: vi.fn(),
  refreshThreads: vi.fn(),
  pushToast: vi.fn(),
  // A tiny stand-in for the Hypervisor store's `config` signal —
  // startChatFromPrompt reads .value and may populate it after a fetch.
  hvConfig: { value: null as { defaultAssistant?: string } | null },
}));

vi.mock('./tasks', () => ({ createTask: (...a: unknown[]) => createTask(...a) }));
vi.mock('./router', () => ({ navigate: (...a: unknown[]) => navigate(...a) }));
vi.mock('./ui', () => ({ pushToast: (...a: unknown[]) => pushToast(...a) }));
vi.mock('../api/hypervisor', () => ({
  createThread: (...a: unknown[]) => createThread(...a),
  getHypervisorConfig: (...a: unknown[]) => getHypervisorConfig(...a),
}));
vi.mock('./hypervisor', () => ({
  config: hvConfig,
  refreshThreads: (...a: unknown[]) => refreshThreads(...a),
}));

import { startBuildFromPrompt, startChatFromPrompt } from './desktop';

beforeEach(() => {
  createTask.mockReset();
  navigate.mockReset();
  createThread.mockReset();
  getHypervisorConfig.mockReset();
  refreshThreads.mockReset();
  pushToast.mockReset();
  hvConfig.value = null;
});

describe('startBuildFromPrompt', () => {
  it('creates a build and navigates into its detail view', async () => {
    createTask.mockResolvedValue({ task_id: 'abc123' });
    const ok = await startBuildFromPrompt('  add dark mode  ', '/home/dev');
    expect(ok).toBe(true);
    // Prompt is trimmed before it reaches the API.
    expect(createTask).toHaveBeenCalledWith({
      prompt: 'add dark mode',
      workdir: '/home/dev',
      assistant: undefined,
    });
    expect(navigate).toHaveBeenCalledWith('/tasks/abc123');
  });

  it('passes an explicit assistant through when provided', async () => {
    createTask.mockResolvedValue({ task_id: 'x1' });
    await startBuildFromPrompt('do it', '/home/dev/kube-coder', 'opencode-openrouter');
    expect(createTask).toHaveBeenCalledWith({
      prompt: 'do it',
      workdir: '/home/dev/kube-coder',
      assistant: 'opencode-openrouter',
    });
  });

  it('is a no-op for an empty / whitespace prompt', async () => {
    const ok = await startBuildFromPrompt('   ', '/home/dev');
    expect(ok).toBe(false);
    expect(createTask).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it('does not navigate when task creation fails', async () => {
    createTask.mockResolvedValue(null);
    const ok = await startBuildFromPrompt('anything', '/home/dev');
    expect(ok).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
  });

  it('encodes the task id in the destination URL', async () => {
    createTask.mockResolvedValue({ task_id: 'a b/c' });
    await startBuildFromPrompt('x', '/home/dev');
    expect(navigate).toHaveBeenCalledWith('/tasks/a%20b%2Fc');
  });
});

describe('startChatFromPrompt', () => {
  it('creates a chat thread in the given workdir and deep-links into it', async () => {
    getHypervisorConfig.mockResolvedValue({ defaultAssistant: 'claude' });
    createThread.mockResolvedValue({ id: 't-1' });
    const ok = await startChatFromPrompt('  what is running?  ', '/home/dev');
    expect(ok).toBe(true);
    // Prompt trimmed; default assistant resolved from config when none passed.
    expect(createThread).toHaveBeenCalledWith({
      message: 'what is running?',
      assistant: 'claude',
      workdir: '/home/dev',
    });
    expect(refreshThreads).toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith('/hypervisor/t-1');
  });

  it('reuses an already-loaded config without re-fetching', async () => {
    hvConfig.value = { defaultAssistant: 'opencode-openrouter' };
    createThread.mockResolvedValue({ id: 't-2' });
    await startChatFromPrompt('hi', '/home/dev/kube-coder');
    expect(getHypervisorConfig).not.toHaveBeenCalled();
    expect(createThread).toHaveBeenCalledWith({
      message: 'hi',
      assistant: 'opencode-openrouter',
      workdir: '/home/dev/kube-coder',
    });
  });

  it('honors an explicitly passed assistant', async () => {
    createThread.mockResolvedValue({ id: 't-3' });
    await startChatFromPrompt('go', '/home/dev', 'librefang');
    expect(getHypervisorConfig).not.toHaveBeenCalled();
    expect(createThread).toHaveBeenCalledWith({
      message: 'go',
      assistant: 'librefang',
      workdir: '/home/dev',
    });
  });

  it('is a no-op for an empty / whitespace prompt', async () => {
    const ok = await startChatFromPrompt('   ', '/home/dev');
    expect(ok).toBe(false);
    expect(createThread).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it('surfaces a toast and does not navigate when thread creation fails', async () => {
    hvConfig.value = { defaultAssistant: 'claude' };
    createThread.mockRejectedValue(new Error('boom'));
    const ok = await startChatFromPrompt('anything', '/home/dev');
    expect(ok).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
    expect(pushToast).toHaveBeenCalled();
  });

  it('encodes the thread id in the destination URL', async () => {
    hvConfig.value = { defaultAssistant: 'claude' };
    createThread.mockResolvedValue({ id: 'a b/c' });
    await startChatFromPrompt('x', '/home/dev');
    expect(navigate).toHaveBeenCalledWith('/hypervisor/a%20b%2Fc');
  });
});
