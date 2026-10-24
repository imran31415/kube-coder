/**
 * Whether the workspace is reachable right now (#728). One app-wide answer, fed
 * by every API call, so the WakeBanner, the screens and the approval queue
 * agree instead of each guessing from its own last error.
 *
 * Pure (no react-native imports) so the node-side vitest suite covers it.
 */
import { SLEEPING, type DownCode, type Reach } from '../util/workspaceReach';

export interface WorkspaceState {
  reach: Reach;
  /** When `reach` last changed (ms since epoch). */
  since: number;
}

type Listener = (s: WorkspaceState) => void;

let state: WorkspaceState = { reach: 'awake', since: Date.now() };
const listeners = new Set<Listener>();
let awakeWaiters: Array<() => void> = [];

function set(reach: Reach): void {
  if (reach === state.reach) return;
  state = { reach, since: Date.now() };
  for (const l of listeners) l(state);
  if (reach === 'awake') {
    const waiters = awakeWaiters;
    awakeWaiters = [];
    for (const w of waiters) w();
  }
}

export function getWorkspaceState(): WorkspaceState {
  return state;
}

export function subscribeWorkspaceState(l: Listener): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

/** The workspace answered a request. */
export function reportAwake(): void {
  set('awake');
}

/** A request found the workspace away. A wake already in flight keeps its
 *  label: failures are expected until the pod is Ready. "Asleep" is more
 *  specific than "unreachable", so an unexplained failure never downgrades it.
 *  "Stopped" is more specific still and comes only from a source that knows
 *  (the controller, or the keeper refusing a wake). The keeper's away answer
 *  is the same for asleep and stopped, so it cannot contradict it. */
export function reportDown(code: DownCode): void {
  if (state.reach === 'waking' || state.reach === 'stopped') return;
  if (code === SLEEPING) set('asleep');
  else if (state.reach === 'awake') set('unreachable');
}

/** A source that knows more than one failed request (the controller's view of
 *  the Deployment, or a wake that was just accepted). */
export function setReach(reach: Reach): void {
  set(reach);
}

/** Run `fn` once the workspace is awake — immediately if it already is.
 *  Returns a cancel function. */
export function onceAwake(fn: () => void): () => void {
  if (state.reach === 'awake') {
    fn();
    return () => {};
  }
  awakeWaiters.push(fn);
  return () => {
    awakeWaiters = awakeWaiters.filter((w) => w !== fn);
  };
}
