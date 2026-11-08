/**
 * Mobile push notifications — the React Native glue (imports expo-notifications,
 * so this file is never pulled into the node-side vitest; the pure ref→target
 * logic it uses lives in src/util/push.ts and is tested there).
 *
 * Lifecycle:
 *  - `initPush()` is called once from App boot: it installs the foreground
 *    display handler, attaches the tap→navigate listener, routes the tap that
 *    launched the app (if any), and attempts to register this device (a no-op
 *    until the app is connected).
 *  - A cold-start tap (#709) and a warm tap take the same path:
 *    `handleNotificationResponse()`. It routes each response once — whichever
 *    of the launch read and the listener sees it first wins — and a tap that
 *    arrives before the navigator is mounted is parked until App's
 *    `onReady` calls `replayParkedNotificationTap()`.
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
import { Platform } from 'react-native';

import { registerPushToken, unregisterPushToken } from '../api/client';
import { isDesktop } from '../desktop/bridge';
import { getConfig, setPushEnabled } from '../store/config';
import type { PushData } from '../util/push';
import { handleNotificationTap } from './tapRouter';

// Where a tap lands lives in ./tapRouter, shared with the web build.
export { handleNotificationTap, replayParkedNotificationTap } from './tapRouter';

let handlerConfigured = false;
let responseSub: Notifications.EventSubscription | null = null;
let lastRegistered = '';
/** Responses already routed this launch — see `responseKey()`. */
const handledResponses = new Set<string>();

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
    // The desktop app (#833) raises its own notifications from the Feed; it
    // has no Expo push token and must not prompt for web push permission.
    if (isDesktop()) return;
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

/** What makes two sightings of a response the same tap.
 *
 *  The identifier alone is not enough: push_notify sets `collapseId` to the
 *  Feed row id, and on iOS that BECOMES the request identifier, so every
 *  re-send of one alert shares it. Keyed on the identifier alone, a tap on a
 *  repeat later in the same launch would be swallowed. The delivery date tells
 *  those apart, and is the same whether the listener or the launch read
 *  reports the response. */
function responseKey(resp: Notifications.NotificationResponse): string {
  const n = resp.notification;
  return `${n?.request?.identifier ?? ''}@${n?.date ?? ''}`;
}

/** Route one notification response — from the tap listener or from the launch
 *  read in `initPush` — exactly once.
 *
 *  Both can see the same response: on a cold start the listener may also be
 *  told about the tap that launched the app, in either order relative to the
 *  read, and a re-run of App's boot effect reads it again. Without the dedupe
 *  each would navigate, and mark the row read, a second time. */
export function handleNotificationResponse(
  resp: Notifications.NotificationResponse | null | undefined,
): void {
  if (!resp?.notification) return;
  const key = responseKey(resp);
  if (handledResponses.has(key)) return;
  handledResponses.add(key);
  // The dedupe lives in memory, so a JS reload in the same native process (an
  // update, a dev reload) would read this response again and re-route to it.
  // Clearing the native copy stops that. Best-effort: absent on web.
  try {
    Notifications.clearLastNotificationResponse();
  } catch {
    // nothing to clear here
  }
  handleNotificationTap(resp.notification.request?.content?.data as PushData | undefined);
}

/** The tap that launched the app from fully closed, if any (#709). The
 *  listener alone misses it: it is attached after the tap happened. Uses the
 *  synchronous `getLastNotificationResponse` — the `…Async` form is
 *  deprecated in this SDK. */
export function routeLaunchNotification(): void {
  try {
    handleNotificationResponse(Notifications.getLastNotificationResponse());
  } catch {
    // unavailable (web) — no launch tap to route
  }
}

/** Attach the tap→navigate listener. Returns a detach function. */
export function attachNotificationResponseListener(): () => void {
  responseSub?.remove();
  responseSub = Notifications.addNotificationResponseReceivedListener(
    handleNotificationResponse,
  );
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
    routeLaunchNotification();
    void registerForPush();
    return detach;
  } catch {
    // best-effort; the in-app Feed still carries every signal
    return () => {};
  }
}
