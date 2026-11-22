import { describe, expect, it } from 'vitest';
import {
  CHAT_ITEM,
  HOME_SCREEN,
  NAV_GROUPS,
  WORKSPACE_DEFAULT,
  WORKSPACE_TITLE,
  inWorkspace,
  isWorkspaceExpanded,
  splitNavGroups,
  toggleWorkspace,
} from './navGroups';
import { TAB_ROUTES } from './screenRegistry';

/** Every drawer destination: Chat, then everything inside Workspace. */
const flat = [CHAT_ITEM, ...NAV_GROUPS.flatMap((g) => g.items)];

describe('top level: Chat + Workspace', () => {
  it('has exactly two top-level entries, Chat then Workspace', () => {
    expect([CHAT_ITEM.label, WORKSPACE_TITLE]).toEqual(['Chat', 'Workspace']);
    expect(CHAT_ITEM.name).toBe('Hypervisor');
  });

  it('opens on Chat', () => {
    expect(HOME_SCREEN).toBe('Hypervisor');
  });

  it('every tab the navigator registers is reachable from the drawer', () => {
    expect(flat.map((i) => i.name).sort()).toEqual([...TAB_ROUTES].sort());
  });

  it('puts everything except Chat inside Workspace', () => {
    expect(inWorkspace('Hypervisor')).toBe(false);
    for (const name of TAB_ROUTES.filter((n) => n !== 'Hypervisor')) {
      expect(inWorkspace(name), name).toBe(true);
    }
  });
});

describe('Workspace disclosure (mirrors the web rail)', () => {
  it('starts collapsed on Chat', () => {
    expect(isWorkspaceExpanded(WORKSPACE_DEFAULT, 'Hypervisor')).toBe(false);
  });

  it('opens by itself while the active screen lives inside it', () => {
    expect(isWorkspaceExpanded(WORKSPACE_DEFAULT, 'Memory')).toBe(true);
    expect(isWorkspaceExpanded(WORKSPACE_DEFAULT, 'Settings')).toBe(true);
  });

  it('an explicit collapse on such a screen sticks until the user moves', () => {
    const d = toggleWorkspace(WORKSPACE_DEFAULT, 'Memory');
    expect(isWorkspaceExpanded(d, 'Memory')).toBe(false);
    // Moving to another Workspace screen reveals it again.
    expect(isWorkspaceExpanded(d, 'Files')).toBe(true);
  });

  it('an explicit open stays open, Chat included; toggling again closes it', () => {
    const open = toggleWorkspace(WORKSPACE_DEFAULT, 'Hypervisor');
    expect(isWorkspaceExpanded(open, 'Hypervisor')).toBe(true);
    expect(isWorkspaceExpanded(open, 'Memory')).toBe(true);
    expect(isWorkspaceExpanded(toggleWorkspace(open, 'Hypervisor'), 'Hypervisor')).toBe(false);
  });
});

describe('NAV_GROUPS (#267)', () => {
  it('covers every top-level screen exactly once (Chat + Workspace)', () => {
    const names = flat.map((i) => i.name);
    expect([...names].sort()).toEqual(
      [
        'Apps',
        'Board',
        'Controller',
        'Desktop',
        'Docs',
        'Feed',
        'Files',
        'Hypervisor',
        'Memory',
        'Metrics',
        'MissionControl',
        'Security',
        'Settings',
        'Skills',
        'Tasks',
        'Triggers',
        'Walkie',
      ].sort(),
    );
    expect(new Set(names).size).toBe(names.length);
  });

  it('files security scanning under Mission Control, beside Board', () => {
    // Both answer "what needs me?" — Board about someone else's tracker,
    // Security about the app the user is building (#726).
    const mission = NAV_GROUPS.find((g) => g.title === 'Mission Control');
    const names = (mission?.items ?? []).map((i) => i.name);
    expect(names).toContain('Security');
    expect(names).toContain('Board');
  });

  it('uses the same Workspace sub-headings as the web dashboard, plus an untitled tail', () => {
    // "Machine" is the old "Workspace" category, renamed so it does not share
    // a name with the container it now sits in.
    expect(NAV_GROUPS.map((g) => g.title)).toEqual([
      'Mission Control',
      'Machine',
      'Knowledge',
      null,
    ]);
  });

  it('applies the shared display labels (Chat, Builds, Overview)', () => {
    const byName = Object.fromEntries(flat.map((i) => [i.name, i.label]));
    expect(byName.Hypervisor).toBe('Chat');
    expect(byName.Tasks).toBe('Builds');
    expect(byName.MissionControl).toBe('Overview');
    expect(byName.Feed).toBe('Feed');
  });

  it('offers no AI CTO destination — it is a mode of Chat (#683)', () => {
    expect(flat.map((i) => i.name)).not.toContain('Cto');
    // Chat is the top-level entry, not a Workspace member.
    expect(NAV_GROUPS.flatMap((g) => g.items).map((i) => i.name)).not.toContain('Hypervisor');
  });

  it('files Triggers under Mission Control and Docs under Knowledge, like the web rail (#250)', () => {
    const groupOf = (name: string) =>
      NAV_GROUPS.find((g) => g.items.some((i) => i.name === name))?.title;
    expect(groupOf('Triggers')).toBe('Mission Control');
    expect(groupOf('Docs')).toBe('Knowledge');
  });

  it('keeps drawer labels unique so Playwright label navigation stays unambiguous', () => {
    const labels = flat.map((i) => i.label);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it('gates only the Controller entry behind a configured controller', () => {
    expect(flat.filter((i) => i.controller).map((i) => i.name)).toEqual(['Controller']);
  });
});

// The drawer list is taller than any iPhone screen, so anything left in the
// scroller can end up off the fold — and before #542 it ended up outside the
// list's frame entirely, where iOS paints a row but never hit-tests it. These
// lock in that Settings is never at the mercy of that list (#549).
describe('splitNavGroups (#549)', () => {
  it('pins Settings outside the scrolling list', () => {
    const { scrolling, pinned } = splitNavGroups({ controller: false });
    expect(pinned?.items.map((i) => i.name)).toEqual(['Settings']);
    expect(scrolling.flatMap((g) => g.items).map((i) => i.name)).not.toContain('Settings');
  });

  it('pins Controller alongside Settings once a controller is configured', () => {
    const { scrolling, pinned } = splitNavGroups({ controller: true });
    expect(pinned?.items.map((i) => i.name)).toEqual(['Controller', 'Settings']);
    expect(scrolling.flatMap((g) => g.items).map((i) => i.name)).not.toContain('Controller');
  });

  it('hides Controller entirely when no controller is configured', () => {
    const { scrolling, pinned } = splitNavGroups({ controller: false });
    const all = [...scrolling, ...(pinned ? [pinned] : [])].flatMap((g) => g.items);
    expect(all.map((i) => i.name)).not.toContain('Controller');
  });

  it('loses no destination and duplicates none across the split', () => {
    for (const controller of [false, true]) {
      const { scrolling, pinned } = splitNavGroups({ controller });
      const names = [...scrolling, ...(pinned ? [pinned] : [])].flatMap((g) =>
        g.items.map((i) => i.name),
      );
      const expected = NAV_GROUPS.flatMap((g) => g.items)
        .filter((i) => !i.controller || controller)
        .map((i) => i.name);
      expect(names.sort()).toEqual(expected.sort());
      expect(new Set(names).size).toBe(names.length);
    }
  });

  it('leaves only titled groups in the scroller, so every scrolled row has a section', () => {
    const { scrolling } = splitNavGroups({ controller: true });
    expect(scrolling.map((g) => g.title)).toEqual(['Mission Control', 'Machine', 'Knowledge']);
  });

  it('drops empty groups rather than rendering a bare section header', () => {
    const { scrolling, pinned } = splitNavGroups({ controller: false });
    for (const g of [...scrolling, ...(pinned ? [pinned] : [])]) {
      expect(g.items.length).toBeGreaterThan(0);
    }
  });
});
