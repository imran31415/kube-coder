import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  sendTaskKey: vi.fn(),
  getTask: vi.fn(),
  sendFollowup: vi.fn(),
  pushToast: vi.fn(),
}));

// `getTask` is mocked alongside `sendTaskKey` because MessageChat polls for
// the pending-prompt state; a partial module mock makes it undefined and the
// component throws before the Stop button is ever rendered.
vi.mock('../../api/tasks', () => ({
  sendTaskKey: mocks.sendTaskKey,
  getTask: mocks.getTask,
}));
vi.mock('../../store/tasks', () => ({ sendFollowup: mocks.sendFollowup }));
vi.mock('../../store/ui', () => ({ pushToast: mocks.pushToast }));
vi.mock('./TerminalPane', () => ({ TerminalPane: () => <div title="Task terminal" /> }));
// Real signals, not `{ value: … }` literals: the composer both reads and
// assigns draftText/draftAttachments, and the component subscribes by reading
// `.value` during render.
vi.mock('./sessionSignals', () => {
  const { signal } = require('@preact/signals');
  const store = new Map<string, Record<string, unknown>>();
  return {
    getSessionSignals: (taskId: string) => {
      let s = store.get(taskId);
      if (!s) {
        s = {
          phase: signal('ready'),
          scrollMode: signal(false),
          reattachCounter: signal(0),
          pasteRequest: signal(null),
          imagePasteRequest: signal(null),
          draftText: signal(''),
          draftAttachments: signal([]),
        };
        store.set(taskId, s);
      }
      return s;
    },
  };
});
vi.mock('./imageAttach', () => ({
  imagesFromClipboard: () => [],
  isImageFile: () => false,
  uploadTaskImage: vi.fn(),
}));

import { serverMode } from '../../store/server-mode';
import { MessageChat } from './MessageChat';

beforeEach(() => {
  mocks.sendTaskKey.mockReset();
  mocks.sendTaskKey.mockResolvedValue({ ok: true, key: 'escape', delivered: true });
  mocks.getTask.mockReset();
  mocks.getTask.mockResolvedValue({ task_id: 'task-1', status: 'running' });
  mocks.pushToast.mockReset();
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
});

describe('MessageChat interrupt button', () => {
  it('interrupts a running task', async () => {
    render(<MessageChat taskId="task-1" status="running" />);

    fireEvent.click(screen.getByRole('button', { name: /stop/i }));

    // Stop is Escape on the shared key endpoint — not an endpoint of its own.
    await waitFor(() => expect(mocks.sendTaskKey).toHaveBeenCalledWith('task-1', 'escape'));
    expect(mocks.pushToast).toHaveBeenCalledWith('Interrupt sent', { kind: 'warn' });
  });

  it('says so plainly when the turn had already finished', async () => {
    // The fire-and-forget race: Stop lands just after the CLI settles. That is
    // a success, so it must not read like a failure.
    mocks.sendTaskKey.mockResolvedValue({ ok: true, key: 'escape', delivered: false });
    render(<MessageChat taskId="task-1" status="running" />);

    fireEvent.click(screen.getByRole('button', { name: /stop/i }));

    await waitFor(() => expect(mocks.pushToast).toHaveBeenCalled());
    const [message, opts] = mocks.pushToast.mock.calls[0];
    expect(message).toMatch(/already finished/i);
    expect(opts).toEqual({ kind: 'info' });
  });

  it('hides Stop when the task is not actively running', () => {
    render(<MessageChat taskId="task-1" status="waiting-for-input" />);

    expect(screen.queryByRole('button', { name: /stop/i })).toBeNull();
  });

  it('shows Stop disabled in a read-only deployment', () => {
    serverMode.value = { readOnly: true, authed: true, authMode: 'basic', demoShowAll: false };
    render(<MessageChat taskId="task-1" status="running" />);

    const stop = screen.getByRole('button', { name: /stop/i });
    expect(stop).toBeDisabled();
    fireEvent.click(stop);
    expect(mocks.sendTaskKey).not.toHaveBeenCalled();
  });
});
