/**
 * Desktop-app wiring (#833), called once from App boot. A no-op on the phone
 * and in a browser.
 *
 * - Live events: the core's stream status and events feed src/store/
 *   liveEvents, which is what lets polling screens stop polling.
 * - Targets: deep links and notification clicks queue in the core; this
 *   drains the queue into the push tap router (handleNotificationTap), so a
 *   `kubecoder://board/…` link, a notification click and a phone push tap all
 *   land the same way — including the cold-start parking from #709.
 * - Sleep (#728): when the core finds the workspace asleep the WakeBanner
 *   shows; when the user's tap wakes it, the core is told to reconnect now.
 *   Nothing here ever wakes the workspace.
 * - The last screen: the core drops the webview when the window is closed or
 *   long hidden, and a rebuilt UI comes back on the screen it was on
 *   (navSession.ts).
 */
import { handleNotificationTap } from '../push/tapRouter';
import { navigationRef } from '../store/nav';
import { emitLiveEvent, setLiveConnected, type LiveEvent } from '../store/liveEvents';
import { reportDown, subscribeWorkspaceState } from '../store/workspaceState';
import { SLEEPING } from '../util/workspaceReach';
import { invoke, isDesktop, listen, type DesktopTarget, type LiveStatus } from './bridge';
import { pushDataFromTarget } from './deepLink';
import { NAV_STATE_KEY, parseNavState, serializeNavState, type SavedNavState } from './navSession';

/** The saved screen, read as the UI boots; used once, when the navigator is
 *  ready. */
let savedNav: Promise<SavedNavState | null> | null = null;
let saveTimer: ReturnType<typeof setTimeout> | null = null;

function applyStatus(s: LiveStatus): void {
  setLiveConnected(s.mode === 'stream');
  if (s.mode === 'asleep') reportDown(SLEEPING);
}

async function drainTargets(): Promise<void> {
  try {
    const targets = await invoke<DesktopTarget[]>('kc_take_targets');
    for (const t of targets) handleNotificationTap(pushDataFromTarget(t));
  } catch {
    // nothing to route
  }
}

export function initDesktop(): () => void {
  if (!isDesktop()) return () => {};
  const offs: Array<() => void> = [];
  let disposed = false;
  const keep = (p: Promise<() => void>) =>
    p.then((off) => (disposed ? off() : offs.push(off))).catch(() => {});

  keep(listen<LiveStatus>('kc://live', applyStatus));
  keep(listen<LiveEvent>('kc://event', emitLiveEvent));
  keep(listen<null>('kc://targets', () => void drainTargets()));
  invoke<LiveStatus>('kc_live_status').then(applyStatus).catch(() => {});
  savedNav = invoke<string | null>('kc_item_get', { key: NAV_STATE_KEY })
    .then(parseNavState)
    .catch(() => null);
  // Whatever launched the app (a link, a notification) is already queued.
  void drainTargets();

  let last = '';
  offs.push(
    subscribeWorkspaceState((s) => {
      // A wake the user asked for has landed (or is under way): reconnect
      // the stream now instead of at the core's next scheduled check.
      if ((s.reach === 'awake' || s.reach === 'waking') && last !== s.reach) {
        invoke('kc_live_kick').catch(() => {});
      }
      last = s.reach;
    }),
  );

  return () => {
    disposed = true;
    offs.forEach((off) => off());
  };
}

/** App's NavigationContainer `onReady`: on desktop, put a rebuilt UI back on
 *  the screen it was on, THEN route whatever opened the window (a parked
 *  notification click or link), so the target lands on top. Elsewhere, and
 *  on a fresh launch with nothing saved, this is just `replayTap`. */
export function onNavigatorReady(replayTap: () => void): void {
  const pending = savedNav;
  savedNav = null;
  if (!isDesktop() || !pending) {
    replayTap();
    return;
  }
  pending
    .then((state) => {
      if (state && navigationRef.isReady()) {
        // A state from another build of the UI (a route that no longer
        // exists) is the navigator's to refuse; starting on the home screen
        // is the fallback either way.
        try {
          navigationRef.resetRoot(state);
        } catch {
          // keep the home screen
        }
      }
    })
    .finally(replayTap);
}

/** App's NavigationContainer `onStateChange`: remember the screen, at most a
 *  couple of times a second. A no-op outside the desktop app. */
export function saveDesktopNavState(state: unknown): void {
  if (!isDesktop()) return;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    const value = serializeNavState(state);
    if (value) invoke('kc_item_set', { key: NAV_STATE_KEY, value }).catch(() => {});
  }, 400);
}
