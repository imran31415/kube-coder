/**
 * How the Controller screen names a workspace's state (#728).
 *
 * Kept apart from the screen so the node-side vitest suite can cover it: it
 * returns a tone, a key of the theme's colours, rather than importing the
 * theme, which pulls in react-native.
 */
import type { ControllerWorkspace } from '../api/types';

export type StateTone = 'success' | 'info' | 'textFaint' | 'warning' | 'danger' | 'textMuted';

const LABELS: Record<string, { tone: StateTone; label: string }> = {
  running: { tone: 'success', label: 'Running' },
  // Resting, not wrong: the calm blue, never a warning colour.
  sleeping: { tone: 'info', label: 'Sleeping' },
  stopped: { tone: 'textFaint', label: 'Stopped' },
  transitioning: { tone: 'warning', label: 'Working' },
  degraded: { tone: 'danger', label: 'Degraded' },
};

/** The pill for a state. A state this build does not know — a newer
 *  controller's — shows as itself, muted, rather than breaking the row. */
export function stateLabel(state: string): { tone: StateTone; label: string } {
  return LABELS[state] ?? { tone: 'textMuted', label: state };
}

/** What Stop will do, in the words the confirmation shows. */
export function stopQuestion(w: Pick<ControllerWorkspace, 'state' | 'user'>): string {
  return w.state === 'sleeping'
    ? `${w.user} is asleep, so a visit would wake it. Stopping it means it stays off until someone starts it.`
    : `Scale ${w.user} to zero. Its data is preserved and it can be started again.`;
}
