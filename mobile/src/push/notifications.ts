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
 *
 * Everything here is best-effort: a permission denial, a simulator with no push
 * support, or a network hiccup must never break the app — the in-app Feed still
 * carries every signal. Failures are swallowed.
 */
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { Linking, Platform } from 'react-native';

import { registerPushToken, unregisterPushToken } from '../api/client';
import { getConfig } from '../store/config';
import { navigateTo, navigationRef } from '../store/nav';
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
 *  a device, not connected, permission denied, or the token is unchanged. */
export async function registerForPush(): Promise<void> {
  try {
    if (!Device.isDevice) return; // push isn't delivered to simulators/emulators
    const { host, token } = getConfig();
    if (!host || !token) return; // not connected yet — register after onboarding

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
    await registerPushToken(expoToken, Platform.OS === 'ios' ? 'ios' : 'android');
    lastRegistered = expoToken;
  } catch {
    // best-effort; the feed still carries every signal
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

/** Route a notification tap to the screen its ref points at, reusing the Feed's
 *  mapping. Unknown/empty refs open the Feed so a tap is never a dead end. */
export function handleNotificationTap(data: PushData | undefined): void {
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
      navigateTo('Cto');
      break;
    case 'memory':
      navigateTo('Memory');
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

/** One-call init from App boot: display handler + tap routing + registration. */
export function initPush(): () => void {
  configureNotificationHandler();
  const detach = attachNotificationResponseListener();
  void registerForPush();
  return detach;
}
