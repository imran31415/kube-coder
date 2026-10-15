import { useEffect, useRef, useState } from 'react';
import { getWorkspaceState, subscribeWorkspaceState, type WorkspaceState } from './workspaceState';

/** Subscribe a component to the workspace's reachability (#728). */
export function useWorkspaceState(): WorkspaceState {
  const [s, setS] = useState<WorkspaceState>(getWorkspaceState());
  useEffect(() => subscribeWorkspaceState(setS), []);
  return s;
}

/** Run `fn` every time the workspace comes back — a screen showing stale data
 *  refreshes at once instead of waiting for its next poll. */
export function useOnAwake(fn: () => void | Promise<void>): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(
    () =>
      subscribeWorkspaceState((s) => {
        if (s.reach === 'awake') void fnRef.current();
      }),
    [],
  );
}
