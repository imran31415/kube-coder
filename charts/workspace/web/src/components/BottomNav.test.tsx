import { render, screen, waitFor } from '@testing-library/preact';
import { describe, expect, it } from 'vitest';
import { BottomNav } from './BottomNav';
import { currentPath, navigate, navLabel, NAV_TOP, ROUTES } from '../store/router';
import { sheetOpen } from '../store/ui';

describe('BottomNav', () => {
  it('labels the /hypervisor route "Chat" everywhere (#346)', () => {
    render(<BottomNav />);
    expect(screen.getByText('Chat')).toBeInTheDocument();
    // The bar, the rail/palette (navLabel) and the document title (ROUTES)
    // must agree — "Hypervisor" is an internal term only.
    expect(navLabel('/hypervisor')).toBe('Chat');
    expect(ROUTES.find((r) => r.path === '/hypervisor')?.title).toBe('Chat');
  });

  it('has the same two top-level entries as the rail: Chat + Workspace', () => {
    const { container } = render(<BottomNav />);
    const labels = [...container.querySelectorAll('.bn-label')].map((l) => l.textContent);
    expect(labels).toEqual(NAV_TOP.map((t) => t.title));
    expect(labels).toEqual(['Chat', 'Workspace']);
  });

  it('Chat navigates home; Workspace opens the Workspace sheet', () => {
    navigate('/desktop', true);
    render(<BottomNav />);
    screen.getByText('Chat').click();
    expect(currentPath.value).toBe('/hypervisor');
    screen.getByLabelText('Workspace').click();
    expect(sheetOpen.value).toBe('more');
    sheetOpen.value = null;
  });

  it('marks Workspace active for any route inside it, Chat only on Chat', async () => {
    navigate('/memory', true);
    const { container } = render(<BottomNav />);
    const active = () =>
      [...container.querySelectorAll('.bn-item-active .bn-label')].map((l) => l.textContent);
    expect(active()).toEqual(['Workspace']);
    navigate('/hypervisor');
    await waitFor(() => expect(active()).toEqual(['Chat']));
  });

  it('shows no user-facing "Hypervisor" label in the route table', () => {
    expect(ROUTES.map((r) => r.title)).not.toContain('Hypervisor');
  });
});
