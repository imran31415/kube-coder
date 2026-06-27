import { render, screen } from '@testing-library/preact';
import { describe, expect, it, vi } from 'vitest';
import { Drawer } from './Drawer';

describe('Drawer', () => {
  it('renders title + body and is hidden when open=false', () => {
    const onClose = vi.fn();
    render(
      <Drawer open={false} onClose={onClose} title="Settings">
        <p>body content</p>
      </Drawer>,
    );
    // Drawer portals to <body>, so query the document, not the render container.
    const drawer = document.querySelector('aside.drawer') as HTMLElement;
    expect(drawer).toBeTruthy();
    expect(drawer.getAttribute('aria-hidden')).toBe('true');
    expect(drawer.classList.contains('drawer-open')).toBe(false);
    expect(drawer.getAttribute('aria-label')).toBe('Settings');
  });

  it('shows when open=true, closes on Esc and on scrim click', () => {
    const onClose = vi.fn();
    render(
      <Drawer open={true} onClose={onClose} title="Settings">
        <p>body content</p>
      </Drawer>,
    );
    const drawer = document.querySelector('aside.drawer') as HTMLElement;
    expect(drawer.classList.contains('drawer-open')).toBe(true);
    expect(screen.getByText('body content')).toBeInTheDocument();

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(1);

    const scrim = document.querySelector('.drawer-scrim') as HTMLElement;
    expect(scrim).toBeTruthy();
    scrim.click();
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('close button fires onClose', () => {
    const onClose = vi.fn();
    render(
      <Drawer open={true} onClose={onClose} title="Settings">
        <p>body</p>
      </Drawer>,
    );
    screen.getByLabelText('Close drawer').click();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
