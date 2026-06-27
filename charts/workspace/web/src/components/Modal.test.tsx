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
    render(
      <Modal open onClose={onClose} label="X">
        <button type="button">inside</button>
      </Modal>,
    );

    screen.getByText('inside').click();
    expect(onClose).not.toHaveBeenCalled();

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(1);

    // Portaled to <body>, so query the document, not the render container.
    (document.querySelector('.modal-scrim') as HTMLElement).click();
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('portals out of .app-content so the inert background never disables it', () => {
    // Regression for the inert self-disable bug: the dialog must NOT live inside
    // the `.app-content` subtree that useFocusTrap marks `inert`, or it would
    // render but be unfocusable/unclickable in a real browser.
    function Harness({ open }: { open: boolean }) {
      return (
        <div class="app-content">
          <Modal open={open} onClose={() => {}} label="X">
            <button type="button">inside</button>
          </Modal>
        </div>
      );
    }
    const { rerender } = render(<Harness open={false} />);
    rerender(<Harness open />);

    const appContent = document.querySelector('.app-content') as HTMLElement;
    expect(appContent.hasAttribute('inert')).toBe(true);

    const insideBtn = screen.getByText('inside');
    // The dialog is portaled to <body>, a sibling of the inert subtree.
    expect(appContent.contains(insideBtn)).toBe(false);
    expect(insideBtn.closest('[inert]')).toBeNull();
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
