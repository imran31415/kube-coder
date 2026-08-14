import { useState } from 'preact/hooks';
import { filteredItems, itemFilter } from '../../store/boards';
import type { BoardItem, BoardPriority, BoardStatus } from '../../api/boards';

/**
 * The shape of the queue, above the list (#622 follow-up).
 *
 * Status and priority are ORDINAL — reordering OPEN → IN_PROGRESS → ON_HOLD →
 * CLOSED, or URGENT → LOW, changes what the reader is being told. So the
 * encoding is one hue in monotone lightness steps rather than a categorical
 * palette: the reader sees the order in the ink. That also keeps the page
 * monochrome, which is the whole visual language here.
 *
 * The ramp steps are validated, not eyeballed — monotone lightness, adjacent
 * ΔL ≥ 0.06, and a light end that clears the surface at ≥ 2:1 in BOTH modes
 * (the first light-mode ramp failed that at 1.55:1 and was stepped darker).
 *
 * It reads the FILTERED items on purpose: with a filter active the bars answer
 * "what am I looking at", which is the more useful question and makes the
 * filter's effect legible.
 */

const STATUS_ORDER: BoardStatus[] = ['OPEN', 'IN_PROGRESS', 'ON_HOLD', 'CLOSED'];
const PRIORITY_ORDER: BoardPriority[] = ['URGENT', 'HIGH', 'NORMAL', 'LOW'];

const STATUS_LABEL: Record<BoardStatus, string> = {
  OPEN: 'open',
  IN_PROGRESS: 'in progress',
  ON_HOLD: 'on hold',
  CLOSED: 'closed',
};
const PRIORITY_LABEL: Record<BoardPriority, string> = {
  URGENT: 'urgent',
  HIGH: 'high',
  NORMAL: 'normal',
  LOW: 'low',
};

/** One bucket of a bar. `slot` indexes the ordinal ramp; -1 is the unmapped bucket. */
interface Bucket {
  key: string;
  label: string;
  count: number;
  slot: number;
}

/**
 * Buckets items by a normalized enum, preserving the ordinal order and keeping
 * everything the connector could not map in a trailing "unmapped" bucket.
 *
 * Unmapped is NOT dropped: a vendor value outside our four buckets is expected
 * (the model passes it through as raw by design), and silently discarding it
 * would make the bar disagree with the item count directly underneath it.
 */
export function bucketBy<T extends string>(
  items: BoardItem[],
  order: T[],
  field: (i: BoardItem) => { normalized: T | null },
  label: Record<T, string>,
): Bucket[] {
  const counts = new Map<string, number>();
  let unmapped = 0;
  for (const item of items) {
    const n = field(item).normalized;
    if (n && order.includes(n)) counts.set(n, (counts.get(n) ?? 0) + 1);
    else unmapped += 1;
  }
  const out: Bucket[] = order
    .map((k, i) => ({ key: k, label: label[k], count: counts.get(k) ?? 0, slot: i }))
    .filter((b) => b.count > 0);
  if (unmapped > 0) {
    out.push({ key: '__unmapped', label: 'unmapped', count: unmapped, slot: -1 });
  }
  return out;
}

function Bar({ title, buckets, total }: { title: string; buckets: Bucket[]; total: number }) {
  const [hover, setHover] = useState<string | null>(null);
  if (total === 0) return null;

  return (
    <div class="qs-row">
      <h3 class="qs-title">{title}</h3>
      <div
        class="qs-bar"
        role="img"
        aria-label={`${title}: ${buckets
          .map((b) => `${b.count} ${b.label}`)
          .join(', ')}`}
      >
        {buckets.map((b) => {
          const pct = (b.count / total) * 100;
          // A label only rides inside the segment when it genuinely fits;
          // flooding every segment is how direct labels stop working.
          const showLabel = pct >= 12;
          return (
            <span
              key={b.key}
              class={`qs-seg qs-slot-${b.slot < 0 ? 'unmapped' : b.slot}${
                hover === b.key ? ' is-hover' : ''
              }`}
              style={{ width: `${pct}%` }}
              onMouseEnter={() => setHover(b.key)}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover(b.key)}
              onBlur={() => setHover(null)}
              tabIndex={0}
              title={`${b.label} · ${b.count} of ${total} (${Math.round(pct)}%)`}
            >
              {showLabel && <span class="qs-seg-label">{b.count}</span>}
            </span>
          );
        })}
      </div>
      {/* Legend is always present for >= 2 buckets, so identity is never
          carried by the ink step alone. */}
      {buckets.length > 1 && (
        <ul class="qs-legend">
          {buckets.map((b) => (
            <li key={b.key} class={hover === b.key ? 'is-hover' : ''}>
              <span class={`qs-swatch qs-slot-${b.slot < 0 ? 'unmapped' : b.slot}`} />
              {b.label}
              <span class="qs-legend-count mono">{b.count}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function QueueShape() {
  const items = filteredItems.value;
  const total = items.length;
  if (total === 0) return null;

  const status = bucketBy(items, STATUS_ORDER, (i) => i.status, STATUS_LABEL);
  const priority = bucketBy(items, PRIORITY_ORDER, (i) => i.priority, PRIORITY_LABEL);

  return (
    <section class="queue-shape" aria-label="Queue shape">
      <div class="qs-total">
        <span class="qs-total-n mono">{total}</span>
        <span class="qs-total-l">
          {itemFilter.value ? 'matching items' : 'items'}
        </span>
      </div>
      <div class="qs-bars">
        <Bar title="Status" buckets={status} total={total} />
        <Bar title="Priority" buckets={priority} total={total} />
      </div>
    </section>
  );
}
