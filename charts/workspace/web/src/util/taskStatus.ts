import type { TaskStatus } from '../api/tasks';

/**
 * Groupings of Build (task) status. Mirrors `charts/workspace/task_status.py`;
 * `tests/task_status_contract_test.py` fails if the two drift.
 */

/** Its tmux session still exists, so the user can interact with it. */
export const ALIVE_STATUSES = ['running', 'waiting-for-input'] as const satisfies readonly TaskStatus[];

/** Over for good: these never change again. */
export const FINISHED_STATUSES = ['completed', 'error', 'killed', 'terminated'] as const satisfies readonly TaskStatus[];

/** Finished without a result to trust, including a Build a pod restart wiped. */
export const FAILED_STATUSES = ['error', 'killed', 'terminated'] as const satisfies readonly TaskStatus[];

export function isAliveStatus(s: string | undefined): boolean {
  return (ALIVE_STATUSES as readonly (string | undefined)[]).includes(s);
}

export function isFailedStatus(s: string | undefined): boolean {
  return (FAILED_STATUSES as readonly (string | undefined)[]).includes(s);
}
