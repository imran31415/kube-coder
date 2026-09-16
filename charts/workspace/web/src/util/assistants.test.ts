import { describe, expect, it } from 'vitest';
import { isNotReady, pickDefault, readyOr } from './assistants';

/** Readiness rules for listed assistants (#702). */

const CLAUDE = { id: 'claude', default: true };
const DSH = { id: 'deepseek-harness', ready: false, notReadyReason: 'needs a key' };
const ANTE = { id: 'ante', ready: true };

describe('isNotReady', () => {
  it('is true only for an explicit ready:false', () => {
    expect(isNotReady(DSH)).toBe(true);
    expect(isNotReady(ANTE)).toBe(false);
    // Older servers never send `ready` — that must read as ready.
    expect(isNotReady(CLAUDE)).toBe(false);
    expect(isNotReady(undefined)).toBe(false);
    expect(isNotReady(null)).toBe(false);
  });
});

describe('pickDefault', () => {
  it('prefers the flagged default when it is ready', () => {
    expect(pickDefault([ANTE, CLAUDE, DSH])?.id).toBe('claude');
  });

  it('skips a not-ready entry even when it is flagged default or listed first', () => {
    expect(pickDefault([{ ...DSH, default: true }, ANTE])?.id).toBe('ante');
    expect(pickDefault([DSH, ANTE])?.id).toBe('ante');
  });

  it('falls back to the first entry when nothing is ready, and undefined when empty', () => {
    expect(pickDefault([DSH])?.id).toBe('deepseek-harness');
    expect(pickDefault([])).toBeUndefined();
  });
});

describe('readyOr', () => {
  it('keeps a ready wanted id', () => {
    expect(readyOr([CLAUDE, ANTE, DSH], 'ante')).toBe('ante');
  });

  it('replaces a not-ready wanted id with the ready default', () => {
    expect(readyOr([CLAUDE, ANTE, DSH], 'deepseek-harness')).toBe('claude');
  });

  it('keeps an id the list does not know, so callers can show it as unavailable', () => {
    expect(readyOr([CLAUDE], 'gone-provider')).toBe('gone-provider');
  });

  it('picks the default for an empty wanted id', () => {
    expect(readyOr([DSH, ANTE], '')).toBe('ante');
  });
});
