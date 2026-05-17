import { render, screen } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ConfirmDialog, PromptDialog } from './ConfirmDialog';

describe('ConfirmDialog', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <ConfirmDialog open={false} title="Delete file?" onConfirm={() => {}} onCancel={() => {}} />,
    );
    expect(container.querySelector('.cd-dialog')).toBeNull();
  });

  it('renders title + body and fires onConfirm', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open
        title="Delete file?"
        body="This cannot be undone."
        confirmLabel="Delete"
        destructive
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByText('Delete file?')).toBeInTheDocument();
    expect(screen.getByText('This cannot be undone.')).toBeInTheDocument();
    await user.click(screen.getByText('Delete'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('Escape closes via onCancel', async () => {
    const onCancel = vi.fn();
    render(
      <ConfirmDialog open title="X" onConfirm={() => {}} onCancel={onCancel} />,
    );
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

describe('PromptDialog', () => {
  it('confirms with the typed value', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <PromptDialog
        open
        title="Rename"
        initial="old"
        onConfirm={onConfirm}
        onCancel={() => {}}
      />,
    );
    const input = screen.getByDisplayValue('old') as HTMLInputElement;
    await user.clear(input);
    await user.type(input, 'new name');
    await user.click(screen.getByText('Save'));
    expect(onConfirm).toHaveBeenCalledWith('new name');
  });

  it('disables save when value is blank', () => {
    render(
      <PromptDialog open title="X" initial=" " onConfirm={() => {}} onCancel={() => {}} />,
    );
    const save = screen.getByText('Save') as HTMLButtonElement;
    expect(save.disabled).toBe(true);
  });
});
