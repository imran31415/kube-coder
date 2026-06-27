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
    expect(screen.getByText('Go to Build')).toBeInTheDocument();
  });

  it('filters entries as the user types', () => {
    paletteOpen.value = true;
    render(<CommandPalette />);
    const input = screen.getByPlaceholderText('Search tasks, memories, triggers, actions…') as HTMLInputElement;
    fireEvent.input(input, { target: { value: 'mem' } });
    // Memory should remain; Tasks should fall away.
    expect(screen.queryByText('Go to Build')).toBeNull();
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

  it('tracks the active option via aria-activedescendant and roves on ArrowDown', () => {
    paletteOpen.value = true;
    render(<CommandPalette />);
    const input = screen.getByRole('combobox') as HTMLInputElement;
    // First row is active by default and the input points at it.
    expect(input.getAttribute('aria-activedescendant')).toBe('palette-opt-0');
    const firstOption = document.getElementById('palette-opt-0');
    expect(firstOption?.getAttribute('aria-selected')).toBe('true');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input.getAttribute('aria-activedescendant')).toBe('palette-opt-1');
    expect(document.getElementById('palette-opt-1')?.getAttribute('aria-selected')).toBe('true');
    expect(document.getElementById('palette-opt-0')?.getAttribute('aria-selected')).toBe('false');
  });
});
