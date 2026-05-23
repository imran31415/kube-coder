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
  it('renders the brand, search trigger, and the default Build route', () => {
    const { container } = render(<App />);
    expect(screen.getByText('kube-coder')).toBeInTheDocument();
    expect(screen.getByLabelText('Open command palette')).toBeInTheDocument();
    // The Build route no longer carries a route-level <h1> — the header was
    // dropped to reclaim vertical space and the "+ New build" affordance moved
    // into the rail. We assert the route mounted by checking the rail's Build
    // item is marked active.
    const activeRailItem = container.querySelector('.rail .rail-item[aria-current="page"]');
    expect(activeRailItem?.textContent).toContain('Build');
  });

  it('navigates between routes via the rail buttons', async () => {
    const { container } = render(<App />);
    // The bottom nav is also rendered in the DOM (hidden via CSS in real
    // life), so pick the rail-specific button. Resolved by text rather
    // than position so adding a new route to ROUTES doesn't break the
    // test (Desktop landed at index 2, Memory shifted to 3).
    const railItems = Array.from(
      container.querySelectorAll('.rail .rail-item'),
    ) as HTMLButtonElement[];
    const railMemory = railItems.find((el) => el.textContent?.includes('Memory'));
    expect(railMemory, 'rail has a Memory entry').toBeTruthy();
    railMemory!.click();
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
    expect(within(dialog).getByText('Go to Build')).toBeInTheDocument();
  });
});
