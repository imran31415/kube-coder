import { describe, expect, it } from 'vitest';
import type { HypervisorThread } from '../../api/hypervisor';
import {
  filterByMode,
  hasMixedModes,
  personaHint,
  personaLabel,
} from './threadMode';

function thread(id: string, persona = ''): HypervisorThread {
  return {
    id,
    title: id,
    assistant: 'claude',
    status: 'idle',
    created_at: 0,
    updated_at: 0,
    persona,
  };
}

describe('personaLabel', () => {
  it('leaves a plain chat unbadged', () => {
    expect(personaLabel('')).toBe('');
    expect(personaLabel(undefined)).toBe('');
    expect(personaLabel(null)).toBe('');
  });

  it('labels the personas the server actually creates', () => {
    expect(personaLabel('cto')).toBe('CTO');
    expect(personaLabel('board')).toBe('Board');
    expect(personaLabel('board-gen')).toBe('Board gen');
  });

  it('is case-insensitive and ignores personas it does not know', () => {
    expect(personaLabel('CTO')).toBe('CTO');
    expect(personaLabel('something-new')).toBe('');
  });

  it('hints match the labelled personas', () => {
    expect(personaHint('cto')).toContain('CTO');
    expect(personaHint('board')).toContain('Board');
    expect(personaHint('')).toBe('');
  });
});

describe('filterByMode', () => {
  const list = [thread('a'), thread('b', 'cto'), thread('c', 'board'), thread('d')];

  it('shows everything by default — no thread is hidden', () => {
    expect(filterByMode(list, 'all')).toHaveLength(4);
  });

  it('narrows to CTO threads', () => {
    expect(filterByMode(list, 'cto').map((t) => t.id)).toEqual(['b']);
  });

  it('treats Workspace as "no persona", not "not CTO"', () => {
    // A board thread is not a workspace chat — it must not fall into the
    // default bucket just because it isn't the CTO.
    expect(filterByMode(list, 'default').map((t) => t.id)).toEqual(['a', 'd']);
  });

  it('preserves input order', () => {
    expect(filterByMode([thread('z'), thread('y')], 'default').map((t) => t.id)).toEqual([
      'z',
      'y',
    ]);
  });
});

describe('hasMixedModes', () => {
  it('is false for a workspace that has only ever used plain chats', () => {
    expect(hasMixedModes([thread('a'), thread('b')])).toBe(false);
  });

  it('is false for an empty list', () => {
    expect(hasMixedModes([])).toBe(false);
  });

  it('is false when every thread carries the same persona', () => {
    expect(hasMixedModes([thread('a', 'cto'), thread('b', 'cto')])).toBe(false);
  });

  it('is true once both kinds are present', () => {
    expect(hasMixedModes([thread('a'), thread('b', 'cto')])).toBe(true);
  });
});
