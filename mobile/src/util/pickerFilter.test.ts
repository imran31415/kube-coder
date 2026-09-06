import { describe, expect, it } from 'vitest';
import { canUseCustom, filterOptions, type PickerOption } from './pickerFilter';

const PROJECTS: PickerOption[] = [
  { value: 'kube-coder', label: 'kube-coder' },
  { value: 'pool-hall', label: 'Pool Hall', hint: '2 running' },
  { value: 'smush', label: 'smush' },
];

const DIRS: PickerOption[] = [
  { value: '/home/dev/kube-coder', label: 'kube-coder', hint: 'git' },
  { value: '/home/dev/smush', label: 'smush', hint: 'git' },
  { value: '/home/dev/notes', label: 'notes' },
];

describe('filterOptions', () => {
  it('returns everything for an empty query', () => {
    expect(filterOptions(PROJECTS, '')).toHaveLength(3);
    expect(filterOptions(PROJECTS, '   ')).toHaveLength(3);
  });

  it('matches the label, case-insensitively', () => {
    expect(filterOptions(PROJECTS, 'POOL').map((o) => o.value)).toEqual(['pool-hall']);
  });

  it('matches the value as well as the label', () => {
    // The two diverge exactly where it matters: a folder's label is a
    // basename, its value a full path. Matching one only makes a picker that
    // cannot find what the user can see.
    expect(filterOptions(DIRS, '/home/dev/notes').map((o) => o.label)).toEqual(['notes']);
    expect(filterOptions(PROJECTS, 'pool-hall').map((o) => o.label)).toEqual(['Pool Hall']);
  });

  it('never matches the hint', () => {
    // "git" and "2 running" are decoration. Matching them returns rows with no
    // visible reason for being there, which reads as the filter lying.
    expect(filterOptions(DIRS, 'git')).toEqual([]);
    expect(filterOptions(PROJECTS, 'running')).toEqual([]);
  });

  it('puts the clear-selection row first, and keeps it findable', () => {
    const rows = filterOptions(PROJECTS, '', 'Workspace');
    expect(rows[0]).toEqual({ value: '', label: 'Workspace' });
    expect(rows).toHaveLength(4);

    expect(filterOptions(PROJECTS, 'works', 'Workspace').map((o) => o.label)).toEqual([
      'Workspace',
    ]);
  });

  it('drops the clear-selection row when it does not match', () => {
    const rows = filterOptions(PROJECTS, 'smush', 'Workspace');
    expect(rows.map((o) => o.value)).toEqual(['smush']);
  });

  it('has no clear-selection row unless one is asked for', () => {
    expect(filterOptions(PROJECTS, '').every((o) => o.value !== '')).toBe(true);
  });

  it('returns nothing rather than everything when nothing matches', () => {
    // The failure that would make the search feel broken: a miss falling back
    // to the full list looks identical to the filter not working at all.
    expect(filterOptions(PROJECTS, 'zzz')).toEqual([]);
  });
});

describe('canUseCustom', () => {
  const rows = filterOptions(DIRS, '/srv');

  it('offers a typed value that matches nothing', () => {
    // /api/workspace/dirs only lists folders UNDER the server root, so a
    // custom workdir is a real answer that is never in the options.
    expect(canUseCustom('/srv/elsewhere', rows, true)).toBe(true);
  });

  it('stays out of the way when custom values are not allowed', () => {
    expect(canUseCustom('/srv/elsewhere', rows, false)).toBe(false);
  });

  it('does not offer a duplicate of a row that already exists', () => {
    const all = filterOptions(DIRS, '');
    expect(canUseCustom('/home/dev/smush', all, true)).toBe(false);
  });

  it('ignores whitespace-only input', () => {
    expect(canUseCustom('   ', rows, true)).toBe(false);
  });
});
