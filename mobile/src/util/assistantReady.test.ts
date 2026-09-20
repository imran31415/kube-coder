import { describe, expect, it } from 'vitest';
import {
  assistantMarker,
  isAssistantReady,
  missingKeyMessage,
} from './assistantReady';

/**
 * The phone's half of #702 — kept in lockstep with
 * charts/workspace/web/src/util/assistantReady.ts so both surfaces say the
 * same thing about the same agent. The marker is shorter here because it rides
 * a chip, not a dropdown row.
 */

const KEYLESS = {
  id: 'deepseek-harness',
  label: 'DeepSeek Harness',
  ready: false,
  needs: 'DEEPSEEK_API_KEY',
};

describe('assistantReady (mobile)', () => {
  it('reads an absent `ready` field as ready', () => {
    // An older server sends no such field; the app must not decide every agent
    // is broken because it is talking to one.
    expect(isAssistantReady({ id: 'ante' })).toBe(true);
    expect(assistantMarker({ id: 'ante' })).toBe('');
    expect(missingKeyMessage({ id: 'ante' })).toBeNull();
  });

  it('marks and explains a keyless agent, naming the variable', () => {
    expect(isAssistantReady(KEYLESS)).toBe(false);
    expect(assistantMarker(KEYLESS)).toContain('needs key');
    const msg = missingKeyMessage(KEYLESS)!;
    expect(msg).toContain('DeepSeek Harness');
    expect(msg).toContain('DEEPSEEK_API_KEY');
    expect(msg).toContain('Settings');
  });

  it('says nothing about a ready agent or an unknown one', () => {
    expect(missingKeyMessage({ id: 'claude', ready: true })).toBeNull();
    expect(missingKeyMessage(undefined)).toBeNull();
  });
});
