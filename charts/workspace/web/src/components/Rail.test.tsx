import { render, screen, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach } from 'vitest';
import { Rail } from './Rail';
import { currentPath, navigate } from '../store/router';
import { railCollapsed, collapsedRailGroups, previewFullscreen } from '../store/ui';

beforeEach(() => {
  localStorage.clear();
  railCollapsed.value = false;
  previewFullscreen.value = false;
  collapsedRailGroups.value = [];
  navigate('/desktop', true);
});

describe('Rail categories (#267)', () => {
  it('renders the three group headers with items, and Settings as a trailing group', () => {
    const { container } = render(<Rail />);
    expect(screen.getByText('Mission Control')).toBeInTheDocument();
    expect(screen.getByText('Workspace')).toBeInTheDocument();
    expect(screen.getByText('Knowledge')).toBeInTheDocument();
    // /hypervisor shows its display label "Chat"; /tasks shows "Builds".
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByText('Builds')).toBeInTheDocument();
    expect(screen.queryByText('Hypervisor')).toBeNull();
    // Settings is not a category member — it flows as the last group inside
    // .rail-items (#448: no pinned bottom slab that overflow can hide).
    expect(container.querySelector('.rail-bottom')).toBeNull();
    const groups = container.querySelectorAll('.rail-items > .rail-group');
    const last = groups[groups.length - 1];
    expect(last?.querySelector('.rail-divider')).toBeTruthy();
    expect(last?.textContent).toContain('Settings');
    expect(last?.textContent).toContain('Collapse');
  });

  it('clicking the Mission Control label navigates to /mission', () => {
    render(<Rail />);
    screen.getByText('Mission Control').click();
    expect(currentPath.value).toBe('/mission');
  });

  it('collapsing a group hides its items and persists to localStorage', async () => {
    render(<Rail />);
    expect(screen.getByText('Memory')).toBeInTheDocument();
    // Knowledge has no landing page, so the whole header toggles.
    screen.getByText('Knowledge').click();
    await waitFor(() => expect(screen.queryByText('Memory')).toBeNull());
    expect(screen.queryByText('Skills')).toBeNull();
    // Other groups stay expanded.
    expect(screen.getByText('Desktop')).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem('kc.rail.groups.v1')!)).toContain('knowledge');
    // Chevron reflects the disclosure state.
    const header = screen.getByText('Knowledge').closest('button');
    expect(header?.getAttribute('aria-expanded')).toBe('false');
  });

  it('tints the header of a collapsed group that holds the active route', async () => {
    const { container } = render(<Rail />);
    // Active route is /desktop (Workspace). Collapse Workspace.
    screen.getByText('Workspace').click();
    await waitFor(() =>
      expect(container.querySelector('.rail-group-header-active')).toBeTruthy(),
    );
  });

  it('auto-expands the group containing the active route on navigation', async () => {
    render(<Rail />);
    collapsedRailGroups.value = ['knowledge'];
    await waitFor(() => expect(screen.queryByText('Memory')).toBeNull());
    navigate('/memory');
    await waitFor(() => expect(screen.getByText('Memory')).toBeInTheDocument());
    expect(collapsedRailGroups.value).not.toContain('knowledge');
  });

  it('collapsed rail flattens groups to icons with hairline dividers', () => {
    railCollapsed.value = true;
    const { container } = render(<Rail />);
    // No group headers or text labels in icon-only mode.
    expect(screen.queryByText('Workspace')).toBeNull();
    expect(screen.queryByText('Knowledge')).toBeNull();
    // Two dividers separate the three groups, plus one leading the
    // trailing Settings/Collapse group.
    expect(container.querySelectorAll('.rail-divider')).toHaveLength(3);
    // Settings + Collapse flow inside .rail-items, not a pinned slab.
    expect(container.querySelector('.rail-bottom')).toBeNull();
    expect(container.querySelector('.rail-items .rail-toggle')).toBeTruthy();
    // The Mission landing gets an icon slot (leads its group).
    expect(container.querySelector('.rail-item[title="Mission Control"]')).toBeTruthy();
    // Active route still marked for orientation.
    const active = container.querySelector('.rail-item[aria-current="page"]');
    expect(active?.getAttribute('title')).toBe('Desktop');
  });
});
