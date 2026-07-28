import { describe, expect, it } from 'vitest';
import { NAV_GROUPS } from './navGroups';

const flat = NAV_GROUPS.flatMap((g) => g.items);

describe('NAV_GROUPS (#267)', () => {
  it('covers every top-level screen exactly once', () => {
    const names = flat.map((i) => i.name);
    expect([...names].sort()).toEqual(
      [
        'Apps',
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
