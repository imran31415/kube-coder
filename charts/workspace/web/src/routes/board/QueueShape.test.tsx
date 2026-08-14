import { render, screen } from '@testing-library/preact';
import { describe, expect, it, beforeEach } from 'vitest';
import { QueueShape, bucketBy } from './QueueShape';
import { boardItems, itemFilter, selectedBoardId } from '../../store/boards';
import type { BoardItem, BoardPriority, BoardStatus } from '../../api/boards';

function item(
  id: string,
  status: BoardStatus | null,
  priority: BoardPriority | null,
  rawStatus = 'weird',
): BoardItem {
  return {
    id,
    key: `K-${id}`,
    ref: {},
    title: `item ${id}`,
    body: '',
    status: { normalized: status, raw: status ?? rawStatus },
    priority: { normalized: priority, raw: priority ?? 'p?' },
    assignee: {},
    contact: {},
    collection: {},
    tags: [],
    url: '',
    created_at: '',
    updated_at: '',
    raw: {},
  } as unknown as BoardItem;
}

const STATUS_ORDER: BoardStatus[] = ['OPEN', 'IN_PROGRESS', 'ON_HOLD', 'CLOSED'];
const LABEL = {
  OPEN: 'open',
  IN_PROGRESS: 'in progress',
  ON_HOLD: 'on hold',
  CLOSED: 'closed',
} as Record<BoardStatus, string>;

describe('bucketBy', () => {
  it('keeps the ordinal order rather than sorting by count', () => {
    const items = [
      item('1', 'CLOSED', 'LOW'),
      item('2', 'CLOSED', 'LOW'),
      item('3', 'CLOSED', 'LOW'),
      item('4', 'OPEN', 'HIGH'),
    ];
    const got = bucketBy(items, STATUS_ORDER, (i) => i.status, LABEL);
    // OPEN first despite having fewer items — the order carries meaning.
    expect(got.map((b) => b.key)).toEqual(['OPEN', 'CLOSED']);
    expect(got.map((b) => b.slot)).toEqual([0, 3]);
  });

  it('drops empty buckets so the bar has no zero-width segments', () => {
    const got = bucketBy([item('1', 'OPEN', 'HIGH')], STATUS_ORDER, (i) => i.status, LABEL);
    expect(got).toHaveLength(1);
    expect(got[0]).toMatchObject({ key: 'OPEN', count: 1 });
  });

  /**
   * The load-bearing one. An unmapped vendor value passes through as raw by
   * design, so if the chart silently dropped it the bar would disagree with
   * the item count printed directly beneath it.
   */
  it('counts unmapped values instead of discarding them', () => {
    const items = [
      item('1', 'OPEN', 'HIGH'),
      item('2', null, null, 'escalated'),
      item('3', null, null, 'escalated'),
    ];
    const got = bucketBy(items, STATUS_ORDER, (i) => i.status, LABEL);
    const total = got.reduce((n, b) => n + b.count, 0);
    expect(total).toBe(items.length);
    expect(got.at(-1)).toMatchObject({ key: '__unmapped', count: 2, slot: -1 });
  });

  it('puts unmapped last so it never interrupts the ordinal ramp', () => {
    const items = [item('1', null, null), item('2', 'OPEN', 'HIGH'), item('3', 'CLOSED', 'LOW')];
    const got = bucketBy(items, STATUS_ORDER, (i) => i.status, LABEL);
    expect(got.at(-1)?.key).toBe('__unmapped');
  });
});

describe('QueueShape', () => {
  beforeEach(() => {
    itemFilter.value = '';
    selectedBoardId.value = 'b1';
    boardItems.value = {};
  });

  /** `selectedItems` is computed — seed the writable source it derives from. */
  const seed = (items: BoardItem[]) => {
    boardItems.value = {
      b1: { items, complete: true, truncation_reason: '', pages_fetched: 1 },
    } as never;
  };

  it('renders nothing when there are no items', () => {
    seed([]);
    const { container } = render(<QueueShape />);
    expect(container.querySelector('.queue-shape')).toBeNull();
  });

  it('shows the total and a segment per present bucket', () => {
    seed([item('1', 'OPEN', 'URGENT'), item('2', 'OPEN', 'LOW'), item('3', 'CLOSED', 'LOW')]);
    const { container } = render(<QueueShape />);
    expect(screen.getByText('3')).toBeTruthy();
    // status: OPEN + CLOSED, priority: URGENT + LOW  => 4 segments
    expect(container.querySelectorAll('.qs-seg')).toHaveLength(4);
  });

  it('describes each bar for screen readers, so identity is not ink-only', () => {
    seed([item('1', 'OPEN', 'URGENT'), item('2', 'CLOSED', 'LOW')]);
    render(<QueueShape />);
    expect(screen.getByLabelText(/Status: 1 open, 1 closed/)).toBeTruthy();
  });

  it('says "matching items" while a filter is active', () => {
    seed([item('1', 'OPEN', 'HIGH')]);
    itemFilter.value = 'item'; // matches the seeded title
    render(<QueueShape />);
    expect(screen.getByText('matching items')).toBeTruthy();
  });

  /** The bars describe the filtered set, so a filter that matches nothing
   *  leaves nothing to shape — rather than a stale full-board bar. */
  it('renders nothing when the filter excludes every item', () => {
    seed([item('1', 'OPEN', 'HIGH')]);
    itemFilter.value = 'nothing-matches-this';
    const { container } = render(<QueueShape />);
    expect(container.querySelector('.queue-shape')).toBeNull();
  });
});
