import { render, screen } from '@testing-library/preact';
import { describe, expect, it } from 'vitest';
import { cardHref, MissionCard } from './MissionCard';
import type { MissionCard as Card } from '../../api/mission';

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

describe('MissionCard waiting_reason', () => {
  const now = Math.floor(Date.now() / 1000);
  const card = (over: Partial<Card>): Card => ({
    id: 'build:1', ref_id: '1', kind: 'build', state: 'waiting',
    title: 'a build', headline: '', assistant: 'claude', model: '',
    workdir: '', repo: '', branch: '', created_at: now - 600, updated_at: now,
    finished_at: null, waiting_since: now - 120, waiting_prompt: null,
    outcome: null, evidence: [], parent_id: null, children: [],
    ...over,
  });
  const article = (container: Element) => container.querySelector('article') as HTMLElement;

  it('takes an idle card out of the attention treatment', () => {
    const { container } = render(<MissionCard card={card({ waiting_reason: 'idle' })} />);
    expect(article(container).className).toContain('mission-card-idle');
    expect(article(container).className).not.toContain('mission-card-attn');
    expect(screen.getByText('idle')).toBeInTheDocument();
    expect(screen.getByText('2m idle')).toBeInTheDocument();
    expect(article(container).textContent).not.toMatch(/\b(done|finished)\b/i);
  });

  it.each([
    ['question', 'question'],
    ['trust_dialog', 'trust dialog'],
    ['undelivered', 'prompt unsent'],
  ] as const)('keeps a %s card in the attention treatment', (reason, label) => {
    const { container } = render(<MissionCard card={card({ waiting_reason: reason })} />);
    expect(article(container).className).toContain('mission-card-attn');
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText('2m waiting')).toBeInTheDocument();
  });

  it('leaves a waiting card with no reason exactly as it was', () => {
    const { container } = render(<MissionCard card={card({})} />);
    expect(article(container).className).toContain('mission-card-attn');
    expect(container.querySelector('.mission-reason')).toBeNull();
  });
});
