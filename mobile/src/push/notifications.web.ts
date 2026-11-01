/**
 * Push notifications on web: the browser build and the desktop app (#833).
 * Metro picks this file over notifications.ts for `--platform web`, which
 * keeps expo-notifications, expo-device and expo-constants out of the web
 * bundle.
 *
 * Neither web target can receive an Expo push:
 *  - a browser would need web push, which needs `notification.vapidPublicKey`
 *    and a service worker in app.config.ts — neither is configured, so
 *    `getExpoPushTokenAsync` always threw there, after a permission prompt
 *    that led nowhere;
 *  - the desktop app raises its own notifications from the Feed in its Rust
 *    core (desktop/src-tauri/src/feed.rs) and reads the Settings switch
 *    (`kc.pushEnabled`) from there.
 *
 * So registration is a no-op here, and the Settings switch only saves the
 * choice. Taps (desktop notification clicks, `kubecoder://` links) route
 * through the same tap router as the phone.
 *
 * Keep the exports in step with notifications.ts: the callers import one
 * module name and Metro resolves it per platform.
 */
import { setPushEnabled } from '../store/config';

export { handleNotificationTap, replayParkedNotificationTap } from './tapRouter';

export function configureNotificationHandler(): void {}

export async function registerForPush(): Promise<void> {}

export async function setPushNotificationsEnabled(on: boolean): Promise<void> {
  await setPushEnabled(on);
}

export async function unregisterForPush(): Promise<void> {}

export function handleNotificationResponse(): void {}

export function routeLaunchNotification(): void {}

export function attachNotificationResponseListener(): () => void {
  return () => {};
}

export function initPush(): () => void {
  return () => {};
}
