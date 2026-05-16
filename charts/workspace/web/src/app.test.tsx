import { render, screen, within } from '@testing-library/preact';
import { describe, expect, it, beforeEach } from 'vitest';
import { App } from './app';
import { currentPath, navigate } from './store/router';
import { theme, density, paletteOpen, sheetOpen, toasts } from './store/ui';

beforeEach(() => {
  // Reset all global state between tests.
  paletteOpen.value = false;
  sheetOpen.value = null;
  toasts.value = [];
  theme.value = 'system';
  density.value = 'comfortable';
  navigate('/tasks', true);
});

describe('App shell', () => {
  it('renders the brand, search trigger, and the default Tasks route', () => {
    render(<App />);
    expect(screen.getByText('kube-coder')).toBeInTheDocument();
    expect(screen.getByLabelText('Open command palette')).toBeInTheDocument();
    // route-level heading
    expect(screen.getByRole('heading', { level: 1, name: 'Tasks' })).toBeInTheDocument();
  });

  it('navigates between routes via the rail buttons', async () => {
    const { container } = render(<App />);
    // The bottom nav is also rendered in the DOM (hidden via CSS in real life),
    // so pick the rail-specific button.
    const railMemory = container.querySelector('.rail .rail-item:nth-child(2)') as HTMLButtonElement;
    expect(railMemory).toBeTruthy();
    expect(railMemory.textContent).toContain('Memory');
    railMemory.click();
    expect(currentPath.value).toBe('/memory');
    // Heading swaps to the new route (signal-triggered re-render).
    await screen.findByRole('heading', { level: 1, name: 'Memory' });
  });

  it('opens the command palette when the topbar search is clicked', async () => {
    render(<App />);
    screen.getByLabelText('Open command palette').click();
    expect(paletteOpen.value).toBe(true);
    // Signal-triggered re-render is async; wait for the dialog to appear.
    const dialog = await screen.findByRole('dialog', { name: 'Command palette' });
    expect(within(dialog).getByPlaceholderText('Search tasks, memories, triggers, actions…')).toBeInTheDocument();
    expect(within(dialog).getByText('Go to Tasks')).toBeInTheDocument();
  });
});
