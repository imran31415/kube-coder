import { describe, expect, it } from 'vitest';
import { cardHref } from './MissionCard';

describe('cardHref (#467 CTO routing, merged in #683)', () => {
  it('routes a chat card to its own chat', () => {
    expect(cardHref('chat:abc123')).toBe('/hypervisor/abc123');
  });

  it('does so for a CTO chat too, instead of dropping the thread', () => {
    // It used to route to the /cto page, because Chat's list excluded CTO
    // threads and the thread URL had nothing to open. One list (#683) means a
    // card pointing at a conversation can finally open that conversation.
    expect(cardHref('chat:abc123')).toBe('/hypervisor/abc123');
  });

  it('routes build / subagent cards to the task detail', () => {
    expect(cardHref('build:t1')).toBe('/tasks/t1');
    expect(cardHref('subagent:s1')).toBe('/tasks/s1');
  });
});
