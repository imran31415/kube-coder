import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  interruptTask: vi.fn(),
  sendFollowup: vi.fn(),
  pushToast: vi.fn(),
}));

vi.mock('../../api/tasks', () => ({ interruptTask: mocks.interruptTask }));
vi.mock('../../store/tasks', () => ({ sendFollowup: mocks.sendFollowup }));
vi.mock('../../store/ui', () => ({ pushToast: mocks.pushToast }));
vi.mock('./TerminalPane', () => ({ TerminalPane: () => <div title="Task terminal" /> }));
vi.mock('./sessionSignals', () => ({
  getSessionSignals: () => ({
    pasteRequest: { value: null },
    imagePasteRequest: { value: null },
  }),
}));
vi.mock('./imageAttach', () => ({
  imagesFromClipboard: () => [],
  isImageFile: () => false,
  uploadTaskImage: vi.fn(),
}));

import { serverMode } from '../../store/server-mode';
import { MessageChat } from './MessageChat';

beforeEach(() => {
  mocks.interruptTask.mockReset();
  mocks.interruptTask.mockResolvedValue({ task_id: 'task-1', status: 'running' });
  mocks.pushToast.mockReset();
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
});

describe('MessageChat interrupt button', () => {
  it('interrupts a running task', async () => {
    render(<MessageChat taskId="task-1" status="running" />);

    fireEvent.click(screen.getByRole('button', { name: /stop/i }));

    await waitFor(() => expect(mocks.interruptTask).toHaveBeenCalledWith('task-1'));
    expect(mocks.pushToast).toHaveBeenCalledWith('Interrupt sent', { kind: 'warn' });
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
    expect(mocks.interruptTask).not.toHaveBeenCalled();
  });
});
