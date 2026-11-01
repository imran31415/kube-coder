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
 */
import { handleNotificationTap } from '../push/tapRouter';
import { emitLiveEvent, setLiveConnected, type LiveEvent } from '../store/liveEvents';
import { reportDown, subscribeWorkspaceState } from '../store/workspaceState';
import { SLEEPING } from '../util/workspaceReach';
import { invoke, isDesktop, listen, type DesktopTarget, type LiveStatus } from './bridge';
import { pushDataFromTarget } from './deepLink';

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
