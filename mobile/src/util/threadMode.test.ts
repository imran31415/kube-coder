import { describe, expect, it } from 'vitest';
import { MODE_OPTIONS, personaLabel } from './threadMode';

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
});

describe('MODE_OPTIONS', () => {
  it('offers exactly the two a human can choose', () => {
    expect(MODE_OPTIONS.map((o) => o.value)).toEqual(['', 'cto']);
  });

  it('does not offer the machine-created board personas', () => {
    // They get a read-only badge instead — a human clicking "New chat" is
    // never what creates one.
    expect(MODE_OPTIONS.map((o) => o.value)).not.toContain('board');
  });
});
