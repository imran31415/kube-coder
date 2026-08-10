import { computed } from '@preact/signals';
import { tasks } from '../store/tasks';
import { feedItems } from '../store/feed';
import { navigate } from '../store/router';
import { drawerOpen, sheetOpen, paletteOpen } from '../store/ui';
import './WaitingBadge.css';

/**
 * Computed signal: which tasks are paused waiting for human input. We
 * piggyback on the existing tasks poll instead of adding a second one —
 * a 10s lag on the badge is acceptable because the user is still going
 * to see the pill on the row itself once they look at the list.
 *
 * Re-exported for Shell.tsx so the document-title effect lives in the same
 * place that sets the base route title — avoids the two effects racing.
 */
export const waitingTasks = computed(() =>
  tasks.value.filter((t) => t.status === 'waiting-for-input'),
);

/**
 * Board items whose agent asked for a human (#588 Phase 5).
 *
 * Derived from the FEED rather than from a second poll of the boards API:
 * `BoardReviewManager` already emits a waiting feed item per item needing
 * review, the feed is already polled, and reading the boards API on a timer
 * would be worse than redundant — item fetches are outbound calls against
 * someone else's rate limit.
 */
export const boardsAwaitingReview = computed(() =>
  feedItems.value.filter(
    (f) =>
      f.waiting &&
      !f.read &&
      (f.links ?? []).some((l) => (l.ref ?? '').startsWith('board:')),
  ),
);

/**
 * Topbar badge — shows in red/warn when anything is paused for a human: a task
 * stopped for input, or a board item whose agent staged a write and wants
 * sign-off. One badge rather than two, because "something needs you" is one
 * question and the count is the answer.
 *
 * Clicking jumps to the first waiting thing, tasks first — a stopped task is
 * blocking a build, while a staged comment is blocking nothing.
 */
export function WaitingBadge() {
  const waiting = waitingTasks.value;
  const reviews = boardsAwaitingReview.value;
  const total = waiting.length + reviews.length;
  if (total === 0) return null;

  function onClick() {
    drawerOpen.value = null;
    sheetOpen.value = null;
    paletteOpen.value = false;
    if (waiting.length > 0) {
      navigate(`/tasks/${waiting[0].task_id}`);
      return;
    }
    const ref =
      (reviews[0].links ?? []).find((l) => (l.ref ?? '').startsWith('board:'))
        ?.ref ?? '';
    const [, rest] = ref.split(/:(.*)/s);
    const [boardId, itemId] = (rest ?? '').split(/:(.*)/s);
    navigate(
      `/board?board=${encodeURIComponent(boardId ?? '')}&review=${encodeURIComponent(itemId ?? '')}`,
    );
  }

  const parts: string[] = [];
  if (waiting.length) {
    parts.push(
      waiting.length === 1 ? '1 task' : `${waiting.length} tasks`,
    );
  }
  if (reviews.length) {
    parts.push(
      reviews.length === 1 ? '1 board item' : `${reviews.length} board items`,
    );
  }
  const label = `${parts.join(' and ')} ${
    total === 1 ? 'is' : 'are'
  } waiting for you`;

  return (
    <button
      type="button"
      class="waiting-badge"
      onClick={onClick}
      title={label}
      aria-label={label}
    >
      <span class="waiting-badge-dot" aria-hidden="true" />
      <span class="waiting-badge-count mono">{total}</span>
      <span class="waiting-badge-label">waiting</span>
    </button>
  );
}

