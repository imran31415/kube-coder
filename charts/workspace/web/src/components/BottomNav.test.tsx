import { render, screen } from '@testing-library/preact';
import { describe, expect, it } from 'vitest';
import { BottomNav } from './BottomNav';
import { navLabel, ROUTES } from '../store/router';

describe('BottomNav', () => {
  it('labels the /hypervisor route "Chat" everywhere (#346)', () => {
    render(<BottomNav />);
    expect(screen.getByText('Chat')).toBeInTheDocument();
    // The bar, the rail/palette (navLabel) and the document title (ROUTES)
    // must agree — "Hypervisor" is an internal term only.
    expect(navLabel('/hypervisor')).toBe('Chat');
    expect(ROUTES.find((r) => r.path === '/hypervisor')?.title).toBe('Chat');
  });

  it('takes every slot label from navLabel so nav surfaces cannot drift', () => {
    render(<BottomNav />);
    for (const path of ['/desktop', '/hypervisor', '/tasks', '/memory']) {
      expect(screen.getByText(navLabel(path))).toBeInTheDocument();
    }
  });

  it('shows no user-facing "Hypervisor" label in the route table', () => {
    expect(ROUTES.map((r) => r.title)).not.toContain('Hypervisor');
  });
});
