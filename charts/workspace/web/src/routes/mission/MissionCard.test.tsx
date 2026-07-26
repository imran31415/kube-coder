import { describe, expect, it } from 'vitest';
import { cardHref } from './MissionCard';

describe('cardHref (#467 CTO routing)', () => {
  it('routes a plain chat card to the Hypervisor tab', () => {
    expect(cardHref('chat:abc123')).toBe('/hypervisor/abc123');
    expect(cardHref('chat:abc123', '')).toBe('/hypervisor/abc123');
  });

  it('routes an AI-CTO chat card to /cto', () => {
    expect(cardHref('chat:abc123', 'cto')).toBe('/cto');
  });

  it('routes build / subagent cards to the task detail regardless of persona', () => {
    expect(cardHref('build:t1')).toBe('/tasks/t1');
    expect(cardHref('subagent:s1', 'cto')).toBe('/tasks/s1');
  });
});
