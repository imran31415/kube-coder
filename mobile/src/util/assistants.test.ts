import { describe, expect, it } from 'vitest';
import { isNotReady, notReadyMessage, pickDefault, readyOr } from './assistants';

/** Readiness rules for listed assistants (#702). */

const CLAUDE = { id: 'claude', label: 'Claude Code', default: true };
const DSH = {
  id: 'deepseek-harness',
  label: 'DeepSeek Harness',
  ready: false,
  notReadyReason: 'DeepSeek Harness needs a DeepSeek API key.',
};
const ANTE = { id: 'ante', label: 'Ante CLI', ready: true };

describe('isNotReady', () => {
  it('is true only for an explicit ready:false', () => {
    expect(isNotReady(DSH)).toBe(true);
    expect(isNotReady(ANTE)).toBe(false);
    expect(isNotReady(CLAUDE)).toBe(false); // older servers never send `ready`
    expect(isNotReady(undefined)).toBe(false);
  });
});

describe('pickDefault', () => {
  it('prefers the ready flagged default', () => {
    expect(pickDefault([ANTE, CLAUDE, DSH])?.id).toBe('claude');
  });

  it('skips a not-ready entry that is flagged default or listed first', () => {
    expect(pickDefault([{ ...DSH, default: true }, ANTE])?.id).toBe('ante');
    expect(pickDefault([DSH, ANTE])?.id).toBe('ante');
  });

  it('falls back to the first entry when nothing is ready', () => {
    expect(pickDefault([DSH])?.id).toBe('deepseek-harness');
    expect(pickDefault([])).toBeUndefined();
  });
});

describe('readyOr', () => {
  it('replaces a not-ready id, keeps ready and unknown ids', () => {
    expect(readyOr([CLAUDE, DSH], 'deepseek-harness')).toBe('claude');
    expect(readyOr([CLAUDE, ANTE], 'ante')).toBe('ante');
    expect(readyOr([CLAUDE], 'gone-provider')).toBe('gone-provider');
  });
});

describe('notReadyMessage', () => {
  it('returns the server reason, a fallback, or null', () => {
    expect(notReadyMessage(DSH)).toBe('DeepSeek Harness needs a DeepSeek API key.');
    expect(notReadyMessage({ id: 'x', label: 'X', ready: false })).toBe("X isn't set up yet.");
    expect(notReadyMessage(CLAUDE)).toBeNull();
    expect(notReadyMessage(undefined)).toBeNull();
  });
});
