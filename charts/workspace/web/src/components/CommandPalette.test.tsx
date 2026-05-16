import { render, screen, within, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach } from 'vitest';
import { CommandPalette } from './CommandPalette';
import { paletteOpen } from '../store/ui';

beforeEach(() => {
  paletteOpen.value = false;
});

describe('CommandPalette', () => {
  it('returns null when closed', () => {
    paletteOpen.value = false;
    const { container } = render(<CommandPalette />);
    expect(container.querySelector('.palette')).toBeNull();
  });

  it('renders the palette dialog when opened', () => {
    paletteOpen.value = true;
    render(<CommandPalette />);
    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Search tasks, memories, triggers, actions…')).toBeInTheDocument();
    expect(screen.getByText('Go to Tasks')).toBeInTheDocument();
  });

  it('filters entries as the user types', () => {
    paletteOpen.value = true;
    render(<CommandPalette />);
    const input = screen.getByPlaceholderText('Search tasks, memories, triggers, actions…') as HTMLInputElement;
    fireEvent.input(input, { target: { value: 'mem' } });
    // Memory should remain; Tasks should fall away.
    expect(screen.queryByText('Go to Tasks')).toBeNull();
    expect(screen.getByText('Go to Memory')).toBeInTheDocument();
  });

  it('closes via Esc and tells us so through paletteOpen', () => {
    paletteOpen.value = true;
    render(<CommandPalette />);
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(paletteOpen.value).toBe(false);
  });

  it('selecting a route entry closes the palette', () => {
    paletteOpen.value = true;
    render(<CommandPalette />);
    const dialog = screen.getByRole('dialog', { name: 'Command palette' });
    within(dialog).getByText('Go to Memory').click();
    expect(paletteOpen.value).toBe(false);
  });
});
