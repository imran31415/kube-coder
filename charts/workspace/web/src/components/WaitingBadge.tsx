import { computed } from '@preact/signals';
import { tasks } from '../store/tasks';
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
 * Topbar badge — shows in red/warn when any tasks are paused for input.
 * Clicking jumps to the first waiting task. Hidden when zero.
 */
export function WaitingBadge() {
  const waiting = waitingTasks.value;
  if (waiting.length === 0) return null;
  const target = waiting[0];

  function onClick() {
    drawerOpen.value = null;
    sheetOpen.value = null;
    paletteOpen.value = false;
    navigate(`/tasks/${target.task_id}`);
  }

  const label = waiting.length === 1
    ? '1 task is waiting for your input'
    : `${waiting.length} tasks are waiting for your input`;

  return (
    <button
      type="button"
      class="waiting-badge"
      onClick={onClick}
      title={label}
      aria-label={label}
    >
      <span class="waiting-badge-dot" aria-hidden="true" />
      <span class="waiting-badge-count mono">{waiting.length}</span>
      <span class="waiting-badge-label">waiting</span>
    </button>
  );
}

