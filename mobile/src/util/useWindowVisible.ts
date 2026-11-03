/** Window visibility for components (#833); the state lives in
 *  src/store/windowVisibility.ts. */
import { useEffect, useState } from 'react';
import { AppState, Platform } from 'react-native';
import {
  isWindowVisible,
  setWindowVisible,
  subscribeWindowVisible,
  visibleFromAppState,
} from '../store/windowVisibility';
import { installAnimationPause } from './animationPause';

/** Called once from App boot: follow AppState, and on web pause CSS
 *  animations while the window is hidden. Returns the teardown. */
export function initWindowVisibility(): () => void {
  setWindowVisible(visibleFromAppState(AppState.currentState));
  const sub = AppState.addEventListener('change', (s) => setWindowVisible(visibleFromAppState(s)));
  const offAnim =
    Platform.OS === 'web' && typeof document !== 'undefined'
      ? installAnimationPause(document, isWindowVisible(), subscribeWindowVisible)
      : () => {};
  return () => {
    sub.remove();
    offAnim();
  };
}

/** Whether the window can be seen right now. */
export function useWindowVisible(): boolean {
  const [v, set] = useState(isWindowVisible());
  useEffect(() => {
    const off = subscribeWindowVisible(set);
    set(isWindowVisible()); // a change between render and subscribe (see useConfig)
    return off;
  }, []);
  return v;
}
