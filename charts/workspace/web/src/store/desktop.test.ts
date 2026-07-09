import { describe, expect, it, beforeEach, vi } from 'vitest';

// Stub the two collaborators startBuildFromPrompt leans on: createTask
// (which would otherwise hit the network) and navigate (which mutates
// history). We assert on the call args rather than real side effects.
const createTask = vi.fn();
const navigate = vi.fn();
vi.mock('./tasks', () => ({ createTask: (...a: unknown[]) => createTask(...a) }));
vi.mock('./router', () => ({ navigate: (...a: unknown[]) => navigate(...a) }));

import { startBuildFromPrompt } from './desktop';

beforeEach(() => {
  createTask.mockReset();
  navigate.mockReset();
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
