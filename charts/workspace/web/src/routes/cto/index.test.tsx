import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { CtoRoute } from './index';
import { CTO_RAIL_COLLAPSED_KEY, CTO_BRIEF_COLLAPSED_KEY } from './railSplit';
import { _resetProjectsForTest } from '../../store/projects';
import { closeThread, sending, setChatContext } from '../../store/hypervisor';
import { justOnboarded } from '../../store/onboarding';
import { claudeReady } from '../../store/claude';

// The route mounts several async loaders (config, threads, discover, projects).
// Feed them all benign JSON so nothing rejects; we only assert the deterministic
// first-paint (masthead + Workspace scope + starter chips) which needs no data.
function payloadFor(url: string): unknown {
  if (url.includes('/api/hypervisor/config')) {
    return { enabled: true, assistants: [], defaultAssistant: 'claude', workdir: '/home/dev' };
  }
  if (url.includes('/api/hypervisor/threads')) return { threads: [] };
  if (url.includes('/api/projects/_discover')) return { candidates: [], registered: [] };
  if (url.includes('/api/projects')) return { projects: [] };
  return {};
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  _resetProjectsForTest();
  localStorage.clear();
  globalThis.fetch = vi.fn(async (url: string) => ({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => payloadFor(String(url)),
  })) as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  localStorage.clear();
  closeThread();
  setChatContext('', null);
  _resetProjectsForTest();
  justOnboarded.value = false;
  claudeReady.value = null;
});

describe('CtoRoute', () => {
  it('renders the deterministic first-paint: masthead, Workspace scope, starter chips', () => {
    render(<CtoRoute />);
    expect(screen.getByText('AI CTO')).toBeTruthy();
    // Named by role so the assertion holds in both rail states (#530) — the
    // test viewport (1024px) sits in the auto-collapse band.
    expect(screen.getByRole('button', { name: /Workspace/ })).toBeTruthy();
    // Starter chips render before any LLM call / thread exists (lazy creation).
    expect(screen.getByText('What should I focus on?')).toBeTruthy();
    expect(screen.getByText('What are we building?')).toBeTruthy();
  });

  it('shows the first-win opener + build chips when routed straight from onboarding (#487)', () => {
    justOnboarded.value = true;
    claudeReady.value = true; // enterCto only routes here once Claude is ready
    render(<CtoRoute />);
    expect(screen.getByText(/Tell me in one sentence what you'd like to build/)).toBeTruthy();
    expect(screen.getByText('Portfolio site')).toBeTruthy();
    // The normal returning-user chips are replaced, not shown alongside.
    expect(screen.queryByText('What should I focus on?')).toBeNull();
    // The one-shot flag is consumed on mount so a later visit is normal.
    expect(justOnboarded.value).toBe(false);
  });
});

// ── Collapsible side panes (#530) ────────────────────────────────────────────
// The test viewport is 1024px wide — inside the auto-collapse band (≤1200px)
// but above the 860px stacked breakpoint, i.e. exactly the band the issue is
// about, so these exercise the heuristic and the explicit override.
describe('CtoRoute side panes (#530)', () => {
  it('auto-collapses both panes in the cramped band, giving the chat the width', () => {
    const { container } = render(<CtoRoute />);
    expect(container.querySelector('.cto-rail-collapsed')).toBeTruthy();
    expect(container.querySelector('.cto-brief-tab')).toBeTruthy();
    // No expanded brief and no split handle while the rail is a strip.
    expect(container.querySelector('.cto-brief')).toBeNull();
    expect(container.querySelector('.cto-split-handle')).toBeNull();
    // The chat keeps the whole middle track: rail strip + chat + brief tab.
    const root = container.querySelector('.route-cto') as HTMLElement;
    expect(root.style.gridTemplateColumns).toBe('48px minmax(0, 1fr) 34px');
  });

  it('an explicit expand overrides the heuristic and persists', () => {
    const { container } = render(<CtoRoute />);
    fireEvent.click(screen.getByRole('button', { name: 'Expand projects rail' }));
    fireEvent.click(screen.getByRole('button', { name: /Show project brief/ }));

    expect(container.querySelector('.cto-rail-collapsed')).toBeNull();
    expect(container.querySelector('.cto-brief')).toBeTruthy();
    expect(container.querySelector('.cto-split-handle')).toBeTruthy();
    expect(localStorage.getItem(CTO_RAIL_COLLAPSED_KEY)).toBe('0');
    expect(localStorage.getItem(CTO_BRIEF_COLLAPSED_KEY)).toBe('0');
  });

  it('restores a persisted choice on the next visit, heuristic notwithstanding', () => {
    localStorage.setItem(CTO_RAIL_COLLAPSED_KEY, '0');
    localStorage.setItem(CTO_BRIEF_COLLAPSED_KEY, '0');
    const { container } = render(<CtoRoute />);
    expect(container.querySelector('.cto-rail-collapsed')).toBeNull();
    expect(container.querySelector('.cto-brief')).toBeTruthy();
    expect(container.querySelector('.cto-brief-tab')).toBeNull();
  });

  it('stands the brief aside on the first send, but not when the user pinned it open', async () => {
    // Pretend a wide desktop so nothing is auto-collapsed by width.
    const realMM = window.matchMedia;
    window.matchMedia = ((q: string) =>
      ({
        matches: false,
        media: q,
        addEventListener: () => {},
        removeEventListener: () => {},
      })) as unknown as typeof window.matchMedia;
    try {
      const { container } = render(<CtoRoute />);
      expect(container.querySelector('.cto-brief')).toBeTruthy();

      sending.value = true;
      await waitFor(() => expect(container.querySelector('.cto-brief-tab')).toBeTruthy());

      // Re-opening it is an explicit choice: a later send leaves it alone.
      fireEvent.click(screen.getByRole('button', { name: /Show project brief/ }));
      sending.value = false;
      sending.value = true;
      await waitFor(() => expect(container.querySelector('.cto-brief')).toBeTruthy());
      expect(container.querySelector('.cto-brief-tab')).toBeNull();
    } finally {
      sending.value = false;
      window.matchMedia = realMM;
    }
  });

  it('collapsing an expanded pane persists the collapsed choice too', () => {
    localStorage.setItem(CTO_RAIL_COLLAPSED_KEY, '0');
    localStorage.setItem(CTO_BRIEF_COLLAPSED_KEY, '0');
    const { container } = render(<CtoRoute />);
    fireEvent.click(screen.getByRole('button', { name: 'Collapse projects rail' }));
    fireEvent.click(screen.getByRole('button', { name: 'Collapse project brief' }));
    expect(localStorage.getItem(CTO_RAIL_COLLAPSED_KEY)).toBe('1');
    expect(localStorage.getItem(CTO_BRIEF_COLLAPSED_KEY)).toBe('1');
    expect(container.querySelector('.cto-rail-collapsed')).toBeTruthy();
    expect(container.querySelector('.cto-brief-tab')).toBeTruthy();
  });
});
