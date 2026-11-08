/**
 * Where a notification tap — or a desktop deep link, which is routed the same
 * way (#833) — lands. Split out of src/push/notifications.ts so the desktop
 * and web builds can route taps without the expo-notifications glue, which
 * they never use (see notifications.web.ts). The phone imports it through
 * notifications.ts, unchanged.
 *
 * A tap that arrives before the navigator is mounted (a cold start, #709) is
 * parked until App's `onReady` calls `replayParkedNotificationTap()`.
 */
import { Linking } from 'react-native';

import { markFeedRead } from '../api/client';
import { getConfig, subscribe } from '../store/config';
import { navigateTo, navigationRef } from '../store/nav';
import { requestBoardFocus } from '../store/boardFocus';
import { onceAwake } from '../store/workspaceState';
import { pushTargetFromData, tapRoute, type PushData } from '../util/push';
import { isDownError } from '../util/workspaceReach';

/** The tap that arrived before the navigator was mounted, waiting for
 *  `replayParkedNotificationTap()`. Latest wins: only one screen can open. */
let parkedTap: { data: PushData | undefined } | null = null;

/** Mark a tapped alert's Feed row read. A tap that cold-starts the app arrives
 *  before the saved connection is hydrated, when a request has no host or token
 *  to use — so wait for hydration instead of sending one that cannot land.
 *  A tap on a sleeping workspace (#728) waits for it to wake: losing the read
 *  would silence every later repeat of this alert. */
function markTappedRead(feedId: string): void {
  const send = () => {
    Promise.resolve()
      .then(() => markFeedRead(feedId))
      .catch((e) => {
        if (isDownError(e)) onceAwake(send);
      });
  };
  if (getConfig().loaded) {
    send();
    return;
  }
  const unsubscribe = subscribe((c) => {
    if (!c.loaded) return;
    unsubscribe();
    send();
  });
}

/** Route a notification tap to the screen its ref points at, reusing the Feed's
 *  mapping. Unknown/empty refs open the Feed so a tap is never a dead end. */
export function handleNotificationTap(data: PushData | undefined): void {
  // Reading happens even when navigation can't: the server re-sends a repeat of
  // this alert only once its row has been read (#685).
  const feedId = (data?.feedId || '').trim();
  if (feedId) markTappedRead(feedId);
  if (!navigationRef.isReady()) {
    // A cold start (#709): the app is still on its boot screen, so there is no
    // navigator to send this to yet. Hold it for App's onReady.
    parkedTap = { data };
    return;
  }
  routeTap(data);
}

/** Route the tap parked by `handleNotificationTap`, once the navigator is
 *  mounted. App passes this to NavigationContainer's `onReady`. A no-op when
 *  nothing is parked, so calling it twice cannot navigate twice. */
export function replayParkedNotificationTap(): void {
  if (!parkedTap || !navigationRef.isReady()) return;
  const { data } = parkedTap;
  parkedTap = null;
  routeTap(data);
}

function routeTap(data: PushData | undefined): void {
  const route = tapRoute(pushTargetFromData(data));
  if (route.kind === 'external') {
    Linking.openURL(route.url).catch(() => {});
    return;
  }
  if (route.boardFocus) {
    // Park the target BEFORE navigating. Board is a tab screen that stays
    // mounted, so it may never re-mount to read a route param — it reads
    // the request from the store and clears it once the card is in view.
    requestBoardFocus(route.boardFocus.boardId, route.boardFocus.itemId);
  }
  if (route.screen) {
    // A detail screen inside a stack tab (a Build, a scan). `initial: false`
    // keeps the stack's list beneath it, so the back button has somewhere to go.
    navigationRef.navigate(
      // @ts-expect-error — nested route params are validated at the navigator
      route.tab,
      { screen: route.screen, params: route.params, initial: false },
    );
  } else if (route.params) {
    // @ts-expect-error — tab params are validated at the navigator
    navigationRef.navigate(route.tab, route.params);
  } else {
    navigateTo(route.tab);
  }
}
