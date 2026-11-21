import { render, screen, waitFor, within } from '@testing-library/preact';
import { describe, expect, it, beforeEach } from 'vitest';
import { Rail } from './Rail';
import { currentPath, navigate, NAV_GROUPS, ROUTES } from '../store/router';
import { serverMode } from '../store/server-mode';
import {
  railCollapsed,
  collapsedRailGroups,
  autoExpandedGroup,
  previewFullscreen,
} from '../store/ui';

const initialMode = serverMode.value;

beforeEach(() => {
  localStorage.clear();
  railCollapsed.value = false;
  previewFullscreen.value = false;
  collapsedRailGroups.value = [];
  autoExpandedGroup.value = null;
  serverMode.value = { ...initialMode, scansEnabled: true };
  navigate('/desktop', true);
});

/** The labels of the rail's top-level entries (outside the Workspace body). */
function topLevelLabels(container: Element): string[] {
  return [...container.querySelectorAll('.rail-items > .rail-group')]
    .flatMap((g) => [...g.querySelectorAll(':scope > .rail-item, :scope > .rail-group-header .rail-item')])
    .map((el) => el.querySelector('.rail-item-label')?.textContent ?? '');
}

describe('Rail: Chat + Workspace', () => {
  it('has exactly two top-level entries: Chat, then Workspace', () => {
    const { container } = render(<Rail />);
    expect(topLevelLabels(container)).toEqual(['Chat', 'Workspace']);
    // /hypervisor shows its display label "Chat", never the internal name.
    expect(screen.queryByText('Hypervisor')).toBeNull();
    // Collapse still flows with the nav as the final group (#448).
    expect(container.querySelector('.rail-bottom')).toBeNull();
    const groups = container.querySelectorAll('.rail-items > .rail-group');
    expect(groups[groups.length - 1]?.textContent).toContain('Collapse');
  });

  it('Chat navigates to /hypervisor', () => {
    render(<Rail />);
    screen.getByText('Chat').click();
    expect(currentPath.value).toBe('/hypervisor');
  });

  it('Workspace holds the old categories as sub-headings, then Settings', () => {
    const { container } = render(<Rail />);
    const body = container.querySelector('#rail-workspace-items') as HTMLElement;
    expect(body).toBeTruthy();
    const titles = [...body.querySelectorAll('.rail-section-title')].map((t) => t.textContent);
    expect(titles).toEqual(['Mission Control', 'Machine', 'Knowledge']);
    const w = within(body);
    // Mission Control's landing reads "Overview" and opens /mission.
    w.getByText('Overview').click();
    expect(currentPath.value).toBe('/mission');
    expect(w.getByText('Builds')).toBeInTheDocument();
    // Settings closes Workspace, after the sections.
    const items = [...body.querySelectorAll('.rail-item-label')].map((l) => l.textContent);
    expect(items[items.length - 1]).toBe('Settings');
  });

  it('every navigable route is reachable from the rail', () => {
    const { container } = render(<Rail />);
    const reachable = new Set<string>();
    for (const btn of container.querySelectorAll<HTMLButtonElement>('.rail-item')) {
      if (btn.classList.contains('rail-group-toggle')) continue;
      btn.click();
      reachable.add(currentPath.value);
    }
    // /cto is resolvable but deliberately not a destination (#683).
    const expected = ROUTES.map((r) => r.path).filter((p) => p !== '/cto');
    expect([...reachable].sort()).toEqual(expected.sort());
  });

  it('collapses by default and stays collapsed while on Chat', () => {
    collapsedRailGroups.value = ['workspace'];
    navigate('/hypervisor', true);
    render(<Rail />);
    expect(screen.queryByText('Memory')).toBeNull();
    const toggle = screen.getByText('Workspace').closest('button');
    expect(toggle?.getAttribute('aria-expanded')).toBe('false');
  });

  it('toggling Workspace hides its items and persists to localStorage', async () => {
    navigate('/hypervisor', true);
    render(<Rail />);
    expect(screen.getByText('Memory')).toBeInTheDocument();
    screen.getByText('Workspace').click();
    await waitFor(() => expect(screen.queryByText('Memory')).toBeNull());
    expect(screen.queryByText('Settings')).toBeNull();
    expect(JSON.parse(localStorage.getItem('kc.rail.groups.v2')!)).toEqual(['workspace']);
    expect(screen.getByText('Workspace').closest('button')?.getAttribute('aria-expanded')).toBe(
      'false',
    );
  });

  it('tints the Workspace header when collapsed while the active route is inside it', async () => {
    const { container } = render(<Rail />);
    // Active route is /desktop (inside Workspace). Collapse Workspace.
    screen.getByText('Workspace').click();
    await waitFor(() =>
      expect(container.querySelector('.rail-group-header-active')).toBeTruthy(),
    );
  });

  it('auto-reveals Workspace for an active route inside it without persisting it', async () => {
    collapsedRailGroups.value = ['workspace'];
    navigate('/hypervisor', true);
    render(<Rail />);
    await waitFor(() => expect(screen.queryByText('Memory')).toBeNull());
    navigate('/memory');
    await waitFor(() => expect(screen.getByText('Memory')).toBeInTheDocument());
    // The reveal is transient: the stored preference still says collapsed.
    expect(collapsedRailGroups.value).toEqual(['workspace']);
  });

  it('re-collapses once the active route goes back to Chat', async () => {
    collapsedRailGroups.value = ['workspace'];
    render(<Rail />);
    navigate('/memory');
    await waitFor(() => expect(screen.getByText('Memory')).toBeInTheDocument());
    navigate('/hypervisor');
    await waitFor(() => expect(screen.queryByText('Memory')).toBeNull());
  });

  it('keeps capability gates: gated items drop out of Workspace', () => {
    serverMode.value = { ...initialMode, ctoEnabled: false, boardEnabled: false, scansEnabled: false };
    render(<Rail />);
    expect(screen.queryByText('Feed')).toBeNull();
    expect(screen.queryByText('Board')).toBeNull();
    expect(screen.queryByText('Security')).toBeNull();
    expect(screen.getByText('Builds')).toBeInTheDocument();
  });

  it('collapsed rail: Chat + Workspace icons, Workspace expands to icon runs', () => {
    railCollapsed.value = true;
    const { container } = render(<Rail />);
    // No sub-heading text in icon-only mode.
    expect(screen.queryByText('Knowledge')).toBeNull();
    expect(container.querySelector('.rail-item[title="Chat"]')).toBeTruthy();
    // One divider per section, one before Settings, one before Collapse.
    expect(container.querySelectorAll('.rail-divider')).toHaveLength(NAV_GROUPS.length + 2);
    expect(container.querySelector('.rail-items .rail-toggle')).toBeTruthy();
    // The Mission landing gets an icon slot (leads its run).
    expect(container.querySelector('.rail-item[title="Mission Control"]')).toBeTruthy();
    // Active route still marked for orientation.
    const active = container.querySelector('.rail-item[aria-current="page"]');
    expect(active?.getAttribute('title')).toBe('Desktop');
  });
});
