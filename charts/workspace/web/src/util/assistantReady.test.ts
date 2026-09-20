import { describe, expect, it } from 'vitest';
import {
  assistantMarker,
  isAssistantReady,
  missingKeyMessage,
} from './assistantReady';

/**
 * The one place the #702 marker and sentence are derived. Both web pickers and
 * the phone read these, so the rules are pinned here rather than re-asserted at
 * each call site.
 */

const READY = { id: 'claude', label: 'Claude Code', ready: true };
const KEYLESS = {
  id: 'deepseek-harness',
  label: 'DeepSeek Harness',
  ready: false,
  needs: 'DEEPSEEK_API_KEY',
};

describe('assistantReady', () => {
  it('reads an absent `ready` field as ready', () => {
    // An older server sends no such field. Treating that as "every agent is
    // broken" would be a far worse failure than the one this fixes.
    expect(isAssistantReady({ id: 'ante' })).toBe(true);
    expect(assistantMarker({ id: 'ante' })).toBe('');
    expect(missingKeyMessage({ id: 'ante' })).toBeNull();
  });

  it('says nothing about a ready assistant', () => {
    expect(isAssistantReady(READY)).toBe(true);
    expect(assistantMarker(READY)).toBe('');
    expect(missingKeyMessage(READY)).toBeNull();
  });

  it('marks and explains a keyless one, naming the variable', () => {
    expect(isAssistantReady(KEYLESS)).toBe(false);
    expect(assistantMarker(KEYLESS)).toContain('needs API key');
    const msg = missingKeyMessage(KEYLESS)!;
    expect(msg).toContain('DeepSeek Harness');
    expect(msg).toContain('DEEPSEEK_API_KEY');
    expect(msg).toContain('Settings');
  });

  it('falls back to the id when the entry carries no label', () => {
    expect(missingKeyMessage({ id: 'x-agent', ready: false })).toContain('x-agent');
  });

  it('omits the parenthetical when the server names no variable', () => {
    expect(missingKeyMessage({ id: 'x', label: 'X', ready: false })).toBe(
      'X needs an API key. Add it in Settings → Provider API keys.',
    );
  });

  it('treats a missing entry as ready rather than as an error', () => {
    // A thread can name an agent this workspace no longer lists; there is no
    // key to point at, so the turn reports its own error instead.
    expect(isAssistantReady(undefined)).toBe(true);
    expect(missingKeyMessage(null)).toBeNull();
  });
});
