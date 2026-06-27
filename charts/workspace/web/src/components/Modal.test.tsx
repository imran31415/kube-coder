import { render, screen } from '@testing-library/preact';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { Modal } from './Modal';
import { _resetInertCount } from '../hooks/useFocusTrap';

beforeEach(() => {
  _resetInertCount();
  document.body.innerHTML = '';
});

describe('Modal', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <Modal open={false} onClose={() => {}} label="X">
        <p>body</p>
      </Modal>,
    );
    expect(container.querySelector('.modal-dialog')).toBeNull();
  });

  it('exposes role/aria and renders children when open', () => {
    render(
      <Modal open onClose={() => {}} label="Pin a port">
        <button type="button">Save</button>
      </Modal>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Pin a port' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(screen.getByText('Save')).toBeInTheDocument();
  });

  it('closes on Esc and on scrim click but not on inner click', () => {
    const onClose = vi.fn();
    const { container } = render(
      <Modal open onClose={onClose} label="X">
        <button type="button">inside</button>
      </Modal>,
    );

    screen.getByText('inside').click();
    expect(onClose).not.toHaveBeenCalled();

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(1);

    (container.querySelector('.modal-scrim') as HTMLElement).click();
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('restores focus to the opener when it closes', () => {
    function Harness({ open }: { open: boolean }) {
      return (
        <div>
          <button id="opener-btn" type="button">open</button>
          <Modal open={open} onClose={() => {}} label="X">
            <button type="button">inside</button>
          </Modal>
        </div>
      );
    }
    const { rerender } = render(<Harness open={false} />);
    const opener = document.getElementById('opener-btn') as HTMLButtonElement;
    opener.focus();

    rerender(<Harness open />);
    (screen.getByText('inside') as HTMLButtonElement).focus();

    rerender(<Harness open={false} />);
    expect(document.activeElement).toBe(opener);
  });
});
