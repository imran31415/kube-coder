import { describe, expect, it, beforeEach, vi } from 'vitest';

let subsResult: () => Promise<{ subscriptions: unknown; claude_ready: boolean }>;

vi.mock('../api/subscriptions', () => ({
  getSubscriptions: () => subsResult(),
}));

import { claudeReady, refreshClaudeReady } from './claude';

describe('store/claude', () => {
  beforeEach(() => {
    claudeReady.value = null;
    subsResult = () => Promise.resolve({ subscriptions: {}, claude_ready: true });
  });

  it('sets claudeReady from the server flag', async () => {
    subsResult = () => Promise.resolve({ subscriptions: {}, claude_ready: true });
    await refreshClaudeReady();
    expect(claudeReady.value).toBe(true);

    subsResult = () => Promise.resolve({ subscriptions: {}, claude_ready: false });
    await refreshClaudeReady();
    expect(claudeReady.value).toBe(false);
  });

  it('keeps the last-known value on a transient error (never false-flashes)', async () => {
    claudeReady.value = true;
    subsResult = () => Promise.reject(new Error('network'));
    await refreshClaudeReady();
    expect(claudeReady.value).toBe(true);
  });
});
