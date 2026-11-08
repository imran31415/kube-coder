/**
 * Build (task) status in the app's canonical spelling. Pure, so it can be
 * tested without a React Native runtime. Mirrors
 * `charts/workspace/task_status.py` through `normStatus`;
 * `tests/task_status_contract_test.py` fails if the two drift.
 */

/** Wire status -> the spelling the status pill and screens use. */
export const WIRE_STATUS_ALIASES = {
  'waiting-for-input': 'waiting',
  completed: 'done',
} as const;

/** The session still exists, so the user can send it a message. */
export const ACTIVE_STATUSES = ['running', 'waiting'] as const;

/** Over for good: these never change again. */
export const FINISHED_STATUSES = ['done', 'error', 'killed', 'terminated'] as const;

/** Older spellings of `waiting-for-input` the server may still emit. */
const LEGACY_WAITING = ['waiting_for_input', 'waiting_input'];

export function normStatus(s: string | undefined): string {
  if (s === undefined) return '';
  if (LEGACY_WAITING.includes(s)) return WIRE_STATUS_ALIASES['waiting-for-input'];
  return WIRE_STATUS_ALIASES[s as keyof typeof WIRE_STATUS_ALIASES] ?? s;
}

export function isActiveStatus(s: string | undefined): boolean {
  return (ACTIVE_STATUSES as readonly (string | undefined)[]).includes(s);
}
