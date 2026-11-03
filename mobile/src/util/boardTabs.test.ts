import { describe, expect, it, vi } from 'vitest';
import type { BoardRecentRow, FeedItem } from '../api/types';
import {
  BOARD_TABS,
  classifyBoardRow,
  clearDot,
  decisionRequest,
  markRowRead,
  needsYouCount,
  splitRecent,
  tabA11yLabel,
  type BoardTab,
} from './boardTabs';
import { countBoardsAwaitingReview } from './feed';
import {
  drain,
  enqueue,
  pendingItemIds,
  readQueue,
  QUEUE_KEY,
  type QueueStorage,
  type QueuedApproval,
} from './approvalQueue';

/**
 * The phone Board's three tabs (#709 Part 3).
 *
 * The classification test walks EVERY run-item state × conclusion × review
 * state, against the issue's table written out longhand — the server's
 * `boards/outcome.is_done` is tested against the same table
 * (charts/workspace/tests/boards_outcome_test.py), so the Done tab and the
 * Feed item that gives its rows their dots cannot drift apart.
 */

const ROW_STATES: BoardRecentRow['state'][] = [
  'pending', 'claimed', 'working', 'done', 'failed', 'skipped',
];
const DISPOSITIONS = [
  null, 'completed', 'needs_review', 'needs_rescoping', 'blocked', 'rejected', 'failed',
];
const REVIEW_STATES: BoardRecentRow['review_state'][] = [
  null, 'pending', 'partial', 'approved', 'rejected', 'sent_back',
];

/** The issue's table, longhand. */
function expected(
  state: BoardRecentRow['state'],
  disposition: string | null,
  review: BoardRecentRow['review_state'],
): BoardTab | null {
  const reviewOpen = review === 'pending' || review === 'partial';
  const decided = review === 'approved' || review === 'rejected' || review === 'sent_back';
  const needsHuman = ['needs_review', 'needs_rescoping', 'blocked'].includes(disposition ?? '');
  // Needs you: a write waiting for approval, or a conclusion that needs a human.
  if (reviewOpen) return 'needs_you';
  // Working: claimed or working — even if an earlier review was decided.
  if (state === 'claimed' || state === 'working') return 'working';
  // Done: the approval was decided, or completed / failed.
  if (decided) return 'done';
  if (state === 'failed') return 'done';
  if (state === 'done') return needsHuman ? 'needs_you' : 'done';
  // Queued or skipped: on no tab.
  return null;
}

function row(over: Partial<BoardRecentRow> = {}): BoardRecentRow {
  return {
    board_id: 'acme', item_id: '1', item_key: 'SUP-1', item_title: 'Ticket',
    item_url: 'https://x/1', run_id: 'run-1-ab', run_origin: 'manual',
    task_id: 't1', state: 'done', disposition: 'completed', review_state: null,
    reason: '', error: '', result: 'Completed on SUP-1', updated_at: 100,
    feed_id: 'fd_1', unread: true,
    ...over,
  };
}

function feed(over: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'fd_x', ts: 1, kind: 'activity', title: 'x', body_md: '', source: '',
    project_id: '', links: [], waiting: false, read: false,
    ...over,
  };
}

describe('classifyBoardRow', () => {
  it('puts every state × conclusion × review combination on the right tab', () => {
    let n = 0;
    for (const state of ROW_STATES) {
      for (const disposition of DISPOSITIONS) {
        for (const review_state of REVIEW_STATES) {
          const got = classifyBoardRow({ state, disposition, review_state });
          expect(got, `${state}/${disposition}/${review_state}`).toBe(
            expected(state, disposition, review_state),
          );
          n += 1;
        }
      }
    }
    expect(n).toBe(ROW_STATES.length * DISPOSITIONS.length * REVIEW_STATES.length);
  });

  it('spot checks the table rows the issue names', () => {
    expect(classifyBoardRow({ state: 'done', disposition: 'needs_review', review_state: 'pending' })).toBe('needs_you');
    expect(classifyBoardRow({ state: 'done', disposition: 'blocked', review_state: null })).toBe('needs_you');
    expect(classifyBoardRow({ state: 'claimed', disposition: null, review_state: null })).toBe('working');
    expect(classifyBoardRow({ state: 'working', disposition: null, review_state: 'sent_back' })).toBe('working');
    expect(classifyBoardRow({ state: 'done', disposition: 'completed', review_state: null })).toBe('done');
    expect(classifyBoardRow({ state: 'failed', disposition: 'failed', review_state: null })).toBe('done');
    expect(classifyBoardRow({ state: 'done', disposition: 'needs_review', review_state: 'approved' })).toBe('done');
    // An approval that half-failed stays in front of the human.
    expect(classifyBoardRow({ state: 'done', disposition: 'needs_review', review_state: 'partial' })).toBe('needs_you');
  });
});

describe('splitRecent', () => {
  it('builds Working and Done from a board with a mix of states, newest first', () => {
    const rows = [
      row({ item_id: 'a', state: 'working', disposition: null, updated_at: 10 }),
      row({ item_id: 'b', state: 'claimed', disposition: null, updated_at: 30 }),
      row({ item_id: 'c', state: 'done', disposition: 'completed', updated_at: 20 }),
      row({ item_id: 'd', state: 'failed', disposition: 'failed', updated_at: 40 }),
      row({ item_id: 'e', state: 'done', disposition: 'needs_review', review_state: 'pending' }),
      row({ item_id: 'f', state: 'done', disposition: 'needs_review', review_state: 'rejected', updated_at: 5 }),
    ];
    const { working, done } = splitRecent(rows);
    expect(working.map((r) => r.item_id)).toEqual(['b', 'a']);
    expect(done.map((r) => r.item_id)).toEqual(['d', 'c', 'f']);
    // The open review is on Needs you — the review queue — and on neither list.
    expect([...working, ...done].some((r) => r.item_id === 'e')).toBe(false);
  });
});

describe('the Needs-you badge comes from the Feed', () => {
  const items = [
    feed({ id: '1', waiting: true, links: [{ label: 'x', ref: 'board:acme:1' }] }),
    feed({ id: '2', waiting: true, links: [{ label: 'x', ref: 'board:acme:I_kw:2' }] }),
    // Read: the drawer does not count it either.
    feed({ id: '3', waiting: true, read: true, links: [{ label: 'x', ref: 'board:acme:3' }] }),
    // Another board.
    feed({ id: '4', waiting: true, links: [{ label: 'x', ref: 'board:other:4' }] }),
    // A finished item's row (#709): non-waiting, so it never counts.
    feed({ id: '5', waiting: false, links: [{ label: 'x', ref: 'board:acme:5' }] }),
    // A waiting task, not a board item.
    feed({ id: '6', waiting: true, links: [{ label: 'x', ref: 'task:abc' }] }),
  ];

  it('counts this board’s waiting, unread board rows only', () => {
    expect(needsYouCount(items, 'acme')).toBe(2);
    expect(needsYouCount(items, 'other')).toBe(1);
    expect(needsYouCount(items, 'nope')).toBe(0);
  });

  it('agrees with the drawer badge, narrowed to one board', () => {
    const boards = ['acme', 'other'];
    const total = boards.reduce((n, b) => n + needsYouCount(items, b), 0);
    expect(total).toBe(countBoardsAwaitingReview(items));
  });

  it('drops when a decision turns the waiting row into its result', () => {
    // The server re-emits the SAME row non-waiting (boards/outcome.feed_key).
    const after = items.map((it) => (it.id === '1' ? { ...it, waiting: false } : it));
    expect(needsYouCount(after, 'acme')).toBe(1);
  });
});

describe('the unread dot on Done', () => {
  it('clears through the Feed read API, once', async () => {
    const markRead = vi.fn(async () => {});
    const r = row({ feed_id: 'fd_9', unread: true });
    await expect(clearDot(r, markRead)).resolves.toBe('fd_9');
    expect(markRead).toHaveBeenCalledWith('fd_9');

    const rows = markRowRead([r, row({ item_id: '2', feed_id: 'fd_8' })], 'fd_9');
    expect(rows[0].unread).toBe(false);
    expect(rows[1].unread).toBe(true);

    // Already read, or no Feed item yet: nothing to call.
    markRead.mockClear();
    await expect(clearDot(rows[0], markRead)).resolves.toBeNull();
    await expect(clearDot(row({ feed_id: null }), markRead)).resolves.toBeNull();
    expect(markRead).not.toHaveBeenCalled();
  });

  it('survives a failed mark — the next poll restores the dot', async () => {
    const markRead = vi.fn(async () => {
      throw new Error('offline');
    });
    await expect(clearDot(row(), markRead)).resolves.toBe('fd_1');
  });
});

describe('tab accessibility labels announce their counts', () => {
  it('names the tab and the count', () => {
    expect(tabA11yLabel('needs_you', 2)).toBe('Needs you, 2 waiting');
    expect(tabA11yLabel('working', 1)).toBe('Working, 1 item');
    expect(tabA11yLabel('done', 4, 2)).toBe('Done, 4 items, 2 unread');
    // The Feed could not be read: no count, rather than a wrong one.
    expect(tabA11yLabel('needs_you', null)).toBe('Needs you');
    expect(BOARD_TABS.map((t) => t.label)).toEqual(['Needs you', 'Working', 'Done']);
  });
});

describe('approve / reject still work under the tabs', () => {
  function mkStorage(): QueueStorage {
    const data = new Map<string, string>();
    return {
      async getItem(k) {
        return data.get(k) ?? null;
      },
      async setItem(k, v) {
        data.set(k, v);
      },
    };
  }

  const base = { board_id: 'acme', item_id: '812', content_hash: 'ab12', reason: '' };

  it('sends exactly the bodies the single-list screen sent', () => {
    const entry = (over: Partial<QueuedApproval>): QueuedApproval => ({
      approval_id: 'appr-1', decision: 'approve', queued_at: 0, attempts: 0,
      last_error: '', ...base, ...over,
    });
    expect(decisionRequest(entry({}))).toEqual({ approval_id: 'appr-1', content_hash: 'ab12' });
    expect(decisionRequest(entry({ decision: 'reject' }))).toEqual({ approval_id: 'appr-1' });
    expect(decisionRequest(entry({ decision: 'reject', reason: 'dup' }))).toEqual({
      approval_id: 'appr-1', reason: 'dup',
    });
    expect(decisionRequest(entry({ decision: 'send-back', reason: 'check EU' }))).toEqual({
      approval_id: 'appr-1', note: 'check EU',
    });
  });

  it('queues offline, greys the card, then sends once with the same id', async () => {
    const storage = mkStorage();
    await enqueue(storage, { ...base, decision: 'approve' });
    await enqueue(storage, { ...base, item_id: '815', decision: 'reject' });
    // The Needs-you tab greys both cards from the queue alone.
    expect(pendingItemIds(await readQueue(storage))).toEqual(new Set(['812', '815']));

    // Offline: nothing lands, nothing is dropped.
    const offline = vi.fn(async () => ({ status: 0 }));
    await drain(storage, offline);
    expect((await readQueue(storage)).length).toBe(2);

    // Back online: each decision is sent once, with the id minted at enqueue.
    const sent: { body: ReturnType<typeof decisionRequest>; item: string }[] = [];
    const online = vi.fn(async (e: QueuedApproval) => {
      sent.push({ body: decisionRequest(e), item: e.item_id });
      return { status: 200 };
    });
    const queued = await readQueue(storage);
    await drain(storage, online);
    expect(online).toHaveBeenCalledTimes(2);
    expect(sent.map((s) => s.body.approval_id)).toEqual(queued.map((q) => q.approval_id));
    expect(sent.find((s) => s.item === '812')?.body.content_hash).toBe('ab12');
    expect(sent.find((s) => s.item === '815')?.body.content_hash).toBeUndefined();
    expect(await storage.getItem(QUEUE_KEY)).toBe('[]');
  });
});
