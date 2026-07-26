import { render, screen, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';

vi.mock('../../store/router', () => ({ navigate: vi.fn() }));
const marked = vi.fn();
const dismissed = vi.fn();
const discussed = vi.fn();
vi.mock('../../store/feed', () => ({
  markRead: (...a: unknown[]) => marked(...a),
  dismiss: (...a: unknown[]) => dismissed(...a),
  discussWithCto: (...a: unknown[]) => discussed(...a),
}));

import { FeedItemView } from './FeedItem';
import { navigate } from '../../store/router';
import type { FeedItem } from '../../api/feed';

const now = Math.floor(Date.now() / 1000);
function item(over: Partial<FeedItem>): FeedItem {
  return {
    id: 'fd_1', ts: now, kind: 'activity', title: 'A thing happened', body_md: '',
    source: 'system:task', project_id: 'kc', links: [], waiting: false, read: false,
    ...over,
  };
}

beforeEach(() => {
  vi.mocked(navigate).mockReset();
  marked.mockReset();
  dismissed.mockReset();
  discussed.mockReset();
  vi.stubGlobal('open', vi.fn());
});
afterEach(() => vi.unstubAllGlobals());

describe('FeedItemView', () => {
  it('applies the kind rule class and shows the unread dot', () => {
    const { container } = render(<FeedItemView item={item({ kind: 'briefing' })} />);
    expect(container.querySelector('.feed-kind-briefing')).toBeTruthy();
    expect(container.querySelector('.feed-unread-dot')).toBeTruthy();
  });

  it('flags a waiting item', () => {
    const { container } = render(<FeedItemView item={item({ waiting: true })} />);
    expect(container.querySelector('.feed-item-waiting')).toBeTruthy();
  });

  it('resolves a task: ref to /tasks/<id> and marks read', () => {
    render(<FeedItemView item={item({ links: [{ label: 'Open task', ref: 'task:t9' }] })} />);
    fireEvent.click(screen.getByText('Open task'));
    expect(marked).toHaveBeenCalledWith('fd_1');
    expect(navigate).toHaveBeenCalledWith('/tasks/t9');
  });

  it('resolves a memory: ref to the Memory tab', () => {
    render(<FeedItemView item={item({ links: [{ label: 'View decision', ref: 'memory:project.kc/x' }] })} />);
    fireEvent.click(screen.getByText('View decision'));
    expect(navigate).toHaveBeenCalledWith('/memory');
  });

  it('opens an external href in a new tab', () => {
    render(<FeedItemView item={item({ links: [{ label: 'Release notes', href: 'https://x/y' }] })} />);
    fireEvent.click(screen.getByText(/Release notes/));
    expect(window.open).toHaveBeenCalledWith('https://x/y', '_blank', 'noopener,noreferrer');
  });

  it('the Discuss with CTO action hands off the item', () => {
    render(<FeedItemView item={item({})} />);
    fireEvent.click(screen.getByText('Discuss with CTO'));
    expect(discussed).toHaveBeenCalledOnce();
  });
});
