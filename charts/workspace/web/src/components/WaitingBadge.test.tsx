import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/preact';
import { WaitingBadge, waitingTasks, boardsAwaitingReview } from './WaitingBadge';
import { tasks } from '../store/tasks';
import { feedItems } from '../store/feed';
import { currentPath } from '../store/router';
import type { TaskSummary } from '../api/tasks';
import type { FeedItem } from '../api/feed';

const sample: TaskSummary[] = [
  {
    task_id: 'live-1', name: 'live one', prompt: 'hi',
    status: 'running', created_at: 1, finished_at: null,
    source: null, kind: 'claude',
    memory_injected: [], memory_injection_disabled: false,
  },
  {
    task_id: 'waiting-1', name: 'awaiting', prompt: 'paused on prompt',
    status: 'waiting-for-input', created_at: 2, finished_at: null,
    source: null, kind: 'claude',
    memory_injected: [], memory_injection_disabled: false,
  },
];

beforeEach(() => {
  tasks.value = [];
});
afterEach(() => {
  tasks.value = [];
});

describe('WaitingBadge', () => {
  it('renders nothing when no tasks are waiting', () => {
    tasks.value = [sample[0]];
    const r = render(<WaitingBadge />);
    expect(r.container.querySelector('.waiting-badge')).toBeNull();
  });

  it('renders with the waiting count when at least one task is paused', () => {
    tasks.value = sample;
    const r = render(<WaitingBadge />);
    const btn = r.container.querySelector('.waiting-badge') as HTMLButtonElement | null;
    expect(btn).not.toBeNull();
    expect(btn!.textContent).toMatch(/1/);
    expect(btn!.getAttribute('aria-label')).toMatch(/1 task is waiting/);
  });

  it('pluralises the aria-label when more than one task is waiting', () => {
    tasks.value = [
      sample[1],
      { ...sample[1], task_id: 'waiting-2' },
    ];
    const r = render(<WaitingBadge />);
    const btn = r.container.querySelector('.waiting-badge') as HTMLButtonElement;
    expect(btn.getAttribute('aria-label')).toMatch(/2 tasks are waiting/);
  });

  it('clicking the badge navigates to the first waiting task', () => {
    tasks.value = sample;
    currentPath.value = '/desktop';
    const r = render(<WaitingBadge />);
    const btn = r.container.querySelector('.waiting-badge') as HTMLButtonElement;
    btn.click();
    expect(currentPath.value).toBe('/tasks/waiting-1');
  });

  it('waitingTasks computed signal reflects the tasks signal', () => {
    tasks.value = sample;
    expect(waitingTasks.value).toHaveLength(1);
    expect(waitingTasks.value[0].task_id).toBe('waiting-1');
    tasks.value = [];
    expect(waitingTasks.value).toHaveLength(0);
  });
});

/**
 * Board items awaiting review (#588 Phase 5) share this badge rather than
 * getting a second one: "something needs you" is one question, and the count
 * is the answer. They are read off the FEED, which is already polled — reading
 * the boards API on a timer would spend someone else's rate limit.
 */
describe('WaitingBadge — board reviews', () => {
  function mkFeed(id: string, ref = 'board:acme-jira:46'): FeedItem {
    return {
      id,
      ts: 1,
      kind: 'activity',
      title: 'SUP-812 needs your review',
      body_md: '',
      source: 'board:acme-jira',
      project_id: '',
      links: [{ ref, label: 'Refund not received' }],
      waiting: true,
      read: false,
      dedupe_key: `board:acme-jira:46`,
    } as FeedItem;
  }

  afterEach(() => {
    feedItems.value = [];
  });

  it('counts a board item waiting for review', () => {
    feedItems.value = [mkFeed('f1')];
    const r = render(<WaitingBadge />);
    const btn = r.container.querySelector('.waiting-badge') as HTMLButtonElement;
    expect(btn).not.toBeNull();
    expect(btn.getAttribute('aria-label')).toMatch(/1 board item is waiting/);
  });

  it('adds board reviews to the task count in one badge', () => {
    tasks.value = [sample[1]];
    feedItems.value = [mkFeed('f1')];
    const r = render(<WaitingBadge />);
    const btn = r.container.querySelector('.waiting-badge') as HTMLButtonElement;
    expect(btn.querySelector('.waiting-badge-count')?.textContent).toBe('2');
    expect(btn.getAttribute('aria-label')).toMatch(
      /1 task and 1 board item are waiting/,
    );
  });

  it('ignores feed items that are read, not waiting, or not board links', () => {
    feedItems.value = [
      { ...mkFeed('f1'), read: true },
      { ...mkFeed('f2'), waiting: false },
      { ...mkFeed('f3'), links: [{ ref: 'task:abc' }] },
    ] as FeedItem[];
    expect(boardsAwaitingReview.value).toHaveLength(0);
    const r = render(<WaitingBadge />);
    expect(r.container.querySelector('.waiting-badge')).toBeNull();
  });

  it('a task outranks a board review when clicking — it blocks a build', () => {
    tasks.value = sample;
    feedItems.value = [mkFeed('f1')];
    currentPath.value = '/desktop';
    const r = render(<WaitingBadge />);
    (r.container.querySelector('.waiting-badge') as HTMLButtonElement).click();
    expect(currentPath.value).toBe('/tasks/waiting-1');
  });

  it('with no waiting task, clicking deep-links to the item under review', () => {
    feedItems.value = [mkFeed('f1')];
    currentPath.value = '/desktop';
    const r = render(<WaitingBadge />);
    (r.container.querySelector('.waiting-badge') as HTMLButtonElement).click();
    expect(currentPath.value).toBe('/board?board=acme-jira&review=46');
  });

  it('keeps an item id that contains colons intact', () => {
    // Linear and Monday hand out `gid://…` ids; splitting on every colon would
    // deep-link to a different item.
    feedItems.value = [mkFeed('f1', 'board:acme:gid://issue/46')];
    currentPath.value = '/desktop';
    const r = render(<WaitingBadge />);
    (r.container.querySelector('.waiting-badge') as HTMLButtonElement).click();
    expect(currentPath.value).toBe(
      '/board?board=acme&review=gid%3A%2F%2Fissue%2F46',
    );
  });
});
