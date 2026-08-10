import { describe, expect, it } from 'vitest';
import { NAV_GROUPS, splitNavGroups } from './navGroups';

const flat = NAV_GROUPS.flatMap((g) => g.items);

describe('NAV_GROUPS (#267)', () => {
  it('covers every top-level screen exactly once', () => {
    const names = flat.map((i) => i.name);
    expect([...names].sort()).toEqual(
      [
        'Apps',
        'Board',
        'Controller',
        'Cto',
        'Desktop',
        'Docs',
        'Feed',
        'Files',
        'Hypervisor',
        'Memory',
        'Metrics',
        'MissionControl',
        'Settings',
        'Skills',
        'Tasks',
        'Triggers',
        'Walkie',
      ].sort(),
    );
    expect(new Set(names).size).toBe(names.length);
  });

  it('uses the same category titles as the web dashboard, plus an untitled tail', () => {
    expect(NAV_GROUPS.map((g) => g.title)).toEqual([
      'Mission Control',
      'Workspace',
      'Knowledge',
      null,
    ]);
  });

  it('applies the shared display labels (Chat, Builds, Overview)', () => {
    const byName = Object.fromEntries(flat.map((i) => [i.name, i.label]));
    expect(byName.Hypervisor).toBe('Chat');
    expect(byName.Tasks).toBe('Builds');
    expect(byName.MissionControl).toBe('Overview');
    expect(byName.Cto).toBe('AI CTO');
    expect(byName.Feed).toBe('Feed');
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
    expect(scrolling.map((g) => g.title)).toEqual(['Mission Control', 'Workspace', 'Knowledge']);
  });

  it('drops empty groups rather than rendering a bare section header', () => {
    const { scrolling, pinned } = splitNavGroups({ controller: false });
    for (const g of [...scrolling, ...(pinned ? [pinned] : [])]) {
      expect(g.items.length).toBeGreaterThan(0);
    }
  });
});
