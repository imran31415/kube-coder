import { render, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

// The embedded terminal does its own attach/network dance — irrelevant here.
vi.mock('./TerminalPane', () => ({ TerminalPane: () => null }));
// Uploads hit /api/files/upload; resolve instantly with a fake on-disk path.
vi.mock('./imageAttach', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./imageAttach')>()),
  uploadTaskImage: vi.fn(() =>
    Promise.resolve('/home/dev/.claude-tasks/t/attachments/pasted-1.png'),
  ),
}));

import { MessageChat } from './MessageChat';
import { serverMode } from '../../store/server-mode';

// Draft persistence across unmount/remount (issue #391): TaskDetail fully
// unmounts MessageChat on every tab switch, so the composer draft (text +
// attachment chips) must live in the per-task session-signal store, not in
// component state. These tests simulate the tab switch by unmounting and
// re-rendering the component.
describe('MessageChat draft persistence', () => {
  beforeEach(() => {
    // The boot default is read-only (public-demo guard) which disables the
    // composer; tests exercise the normal writable workspace.
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
    // happy-dom's object-URL support varies; pin deterministic stubs so we
    // can also assert previews are NOT revoked on unmount.
    URL.createObjectURL = vi.fn(() => 'blob:preview-1');
    URL.revokeObjectURL = vi.fn();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function textarea(container: Element): HTMLTextAreaElement {
    return container.querySelector('textarea.mc-input') as HTMLTextAreaElement;
  }

  it('keeps typed text across an unmount/remount cycle', () => {
    const first = render(<MessageChat taskId="draft-t1" status="completed" />);
    fireEvent.input(textarea(first.container), { target: { value: 'half-typed reply' } });
    first.unmount();

    const second = render(<MessageChat taskId="draft-t1" status="completed" />);
    expect(textarea(second.container).value).toBe('half-typed reply');
  });

  it('keeps attached images (and their previews) across an unmount/remount cycle', async () => {
    const first = render(<MessageChat taskId="draft-t2" status="completed" />);
    const file = new File(['png-bytes'], 'shot.png', { type: 'image/png' });
    const input = first.container.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [file] });
    fireEvent.change(input);
    await waitFor(() => {
      expect(first.container.querySelector('.mc-chip--ready')).not.toBeNull();
    });
    first.unmount();

    const second = render(<MessageChat taskId="draft-t2" status="completed" />);
    const chip = second.container.querySelector('.mc-chip--ready');
    expect(chip).not.toBeNull();
    // The thumbnail's object URL must still be alive — revoking on unmount
    // would leave a broken image when the user returns to the tab.
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    expect((chip!.querySelector('img.mc-chip-thumb') as HTMLImageElement).src)
      .toContain('blob:preview-1');
  });

  it('scopes drafts per task id', () => {
    const first = render(<MessageChat taskId="draft-t3" status="completed" />);
    fireEvent.input(textarea(first.container), { target: { value: 'for task three' } });
    first.unmount();

    const other = render(<MessageChat taskId="draft-t4" status="completed" />);
    expect(textarea(other.container).value).toBe('');
  });
});
