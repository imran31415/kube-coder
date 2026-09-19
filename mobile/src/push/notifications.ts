/**
 * Mobile push notifications — the React Native glue (imports expo-notifications,
 * so this file is never pulled into the node-side vitest; the pure ref→target
 * logic it uses lives in src/util/push.ts and is tested there).
 *
 * Lifecycle:
 *  - `initPush()` is called once from App boot: it installs the foreground
 *    display handler, attaches the tap→navigate listener, and attempts to
 *    register this device (a no-op until the app is connected).
 *  - `registerForPush()` is also called on onboarding success, so a freshly
 *    connected device registers without waiting for the next cold start.
 *  - `setPushNotificationsEnabled()` is the Settings switch (#685): off drops
 *    this phone's registration without disconnecting, on registers it again.
 *    `registerForPush()` does nothing while it is off.
 *  - A tap marks the alert's Feed row read. The server only re-sends a repeat
 *    of an alert once its row has been read (#685), so a tap that did not count
 *    would silence every later repeat of that alert.
 *
 * Everything here is best-effort: a permission denial, a simulator with no push
 * support, or a network hiccup must never break the app — the in-app Feed still
 * carries every signal. Failures are swallowed.
 */
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { Linking, Platform } from 'react-native';

import { markFeedRead, registerPushToken, unregisterPushToken } from '../api/client';
import { getConfig, setPushEnabled, subscribe } from '../store/config';
import { navigateTo, navigationRef } from '../store/nav';
import { requestBoardFocus } from '../store/boardFocus';
import { pushTargetFromData, type PushData } from '../util/push';

let handlerConfigured = false;
let responseSub: Notifications.EventSubscription | null = null;
let lastRegistered = '';

/** Show high-signal pushes while the app is foregrounded (they'd otherwise be
 *  suppressed by the OS on iOS). */
export function configureNotificationHandler(): void {
  if (handlerConfigured) return;
  handlerConfigured = true;
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
    }),
  });
}

function projectId(): string | undefined {
  const fromConfig = Constants.expoConfig?.extra?.eas?.projectId as string | undefined;
  // easConfig is populated in EAS builds; expoConfig.extra covers dev/preview.
  const fromEas = (Constants as unknown as { easConfig?: { projectId?: string } })
    .easConfig?.projectId;
  return fromConfig ?? fromEas;
}

/** Ask for permission (if needed), mint the Expo push token, and register it
 *  with the connected workspace. Safe to call repeatedly — it skips when not on
 *  a device, not connected, switched off in Settings, permission denied, or the
 *  token is unchanged. */
export async function registerForPush(): Promise<void> {
  try {
    if (!Device.isDevice) return; // push isn't delivered to simulators/emulators
    const { host, token, pushEnabled } = getConfig();
    if (!host || !token) return; // not connected yet — register after onboarding
    if (!pushEnabled) return; // switched off: don't even ask for permission

    const existing = await Notifications.getPermissionsAsync();
    let status = existing.status;
    if (status !== 'granted') {
      status = (await Notifications.requestPermissionsAsync()).status;
    }
    if (status !== 'granted') return;

    if (Platform.OS === 'android') {
      await Notifications.setNotificationChannelAsync('default', {
        name: 'default',
        importance: Notifications.AndroidImportance.HIGH,
      });
    }

    const pid = projectId();
    const resp = await Notifications.getExpoPushTokenAsync(pid ? { projectId: pid } : undefined);
    const expoToken = resp.data;
    if (!expoToken || expoToken === lastRegistered) return;
    // The switch can be turned off while we waited on permission or the token.
    if (!getConfig().pushEnabled) return;
    await registerPushToken(expoToken, Platform.OS === 'ios' ? 'ios' : 'android');
    lastRegistered = expoToken;
    // ...or while the register call itself was in flight. The switch's own
    // unregister may already have run and been overtaken by this register, so
    // undo it here rather than leave the workspace pushing to a phone that
    // said no.
    if (!getConfig().pushEnabled) {
      lastRegistered = '';
      await unregisterPushToken(expoToken);
    }
  } catch {
    // best-effort; the feed still carries every signal
  }
}

/** This device's Expo push token without prompting for permission: '' when
 *  push was never granted or is unavailable here (simulator, web). */
async function currentExpoToken(): Promise<string> {
  try {
    if (!Device.isDevice) return '';
    const { status } = await Notifications.getPermissionsAsync();
    if (status !== 'granted') return '';
    const pid = projectId();
    const resp = await Notifications.getExpoPushTokenAsync(pid ? { projectId: pid } : undefined);
    return resp.data || '';
  } catch {
    return '';
  }
}

/** The Settings → Notifications switch (#685).
 *
 *  Off stops the connected workspace pushing to this phone without
 *  disconnecting — before this, disconnecting was the only way. The choice is
 *  saved first, so it holds even when the workspace is unreachable right now:
 *  the unregister is best-effort, and every later launch skips registration.
 *
 *  Off must not depend on `lastRegistered`, which lives in memory only: on a
 *  launch where registration never ran (or failed), it is empty although the
 *  workspace still holds this phone's token. So when it is empty, ask Expo for
 *  the token instead. */
export async function setPushNotificationsEnabled(on: boolean): Promise<void> {
  await setPushEnabled(on);
  if (on) {
    await registerForPush();
    return;
  }
  const token = lastRegistered || (await currentExpoToken());
  lastRegistered = '';
  if (!token) return;
  try {
    await unregisterPushToken(token);
  } catch {
    // best-effort — the saved "off" already stops re-registration
  }
}

/** Drop this device's token from the workspace it is registered with, and
 *  forget it locally. Called on disconnect, BEFORE the saved host/token are
 *  cleared — `unregisterPushToken` needs them to reach the workspace.
 *
 *  Clearing `lastRegistered` is the load-bearing half: without it the next
 *  connection (often a *different* workspace) would see an unchanged Expo token
 *  and skip registration, so push would silently never arrive there. The
 *  network call is best-effort cleanup that stops the old workspace pushing to
 *  a phone that has disconnected from it. */
export async function unregisterForPush(): Promise<void> {
  const token = lastRegistered;
  lastRegistered = '';
  if (!token) return;
  try {
    await unregisterPushToken(token);
  } catch {
    // best-effort — we are disconnecting either way
  }
}

/** Mark a tapped alert's Feed row read. A tap that cold-starts the app arrives
 *  before the saved connection is hydrated, when a request has no host or token
 *  to use — so wait for hydration instead of sending one that cannot land. */
function markTappedRead(feedId: string): void {
  const send = () => {
    Promise.resolve()
      .then(() => markFeedRead(feedId))
      .catch(() => {});
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
  if (!navigationRef.isReady()) return;
  const target = pushTargetFromData(data);
  switch (target.kind) {
    case 'task':
      navigationRef.navigate(
        // @ts-expect-error — nested route params are validated at the navigator
        'Tasks',
        { screen: 'TaskDetail', params: { id: target.id }, initial: false },
      );
      break;
    case 'thread':
      // Opens THAT chat (#683). It used to open the AI CTO screen and drop the
      // id, because that screen had no way to open a thread by reference.
      // @ts-expect-error — tab params are validated at the navigator
      navigationRef.navigate('Hypervisor', { openThreadId: target.id });
      break;
    case 'memory':
      navigateTo('Memory');
      break;
    case 'board':
      // Park the target BEFORE navigating. Board is a tab screen that stays
      // mounted, so it may never re-mount to read a route param — it reads
      // the request from the store and clears it once the card is in view.
      requestBoardFocus(target.boardId, target.itemId);
      navigateTo('Board');
      break;
    case 'external':
      Linking.openURL(target.url).catch(() => {});
      break;
    default:
      navigateTo('Feed');
  }
}

/** Attach the tap→navigate listener. Returns a detach function. */
export function attachNotificationResponseListener(): () => void {
  responseSub?.remove();
  responseSub = Notifications.addNotificationResponseReceivedListener((resp) => {
    handleNotificationTap(
      resp.notification.request.content.data as PushData | undefined,
    );
  });
  return () => {
    responseSub?.remove();
    responseSub = null;
  };
}

/** One-call init from App boot: display handler + tap routing + registration.
 *
 *  Runs inside App's boot effect, so a throw here escapes the effect and takes
 *  the app down before first render — the one failure this module must never
 *  cause. The native calls are therefore guarded like everything else here, and
 *  a failure degrades to "no push" rather than "no app". */
export function initPush(): () => void {
  try {
    configureNotificationHandler();
    const detach = attachNotificationResponseListener();
    void registerForPush();
    return detach;
  } catch {
    // best-effort; the in-app Feed still carries every signal
    return () => {};
  }
}
