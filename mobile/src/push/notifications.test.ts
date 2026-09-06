/**
 * Boot-path guard for the push glue.
 *
 * `initPush()` runs inside App's boot effect, so anything it throws escapes the
 * effect and kills the app before first render. Every other entry point in
 * src/push/notifications.ts is deliberately best-effort; this pins that
 * property for the one function that runs unconditionally at startup.
 *
 * The module imports expo-notifications and react-native, so both are mocked —
 * that is also why the module is otherwise absent from this node-side suite.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const addResponseListener = vi.fn();
const setHandler = vi.fn();

vi.mock('expo-notifications', () => ({
  setNotificationHandler: (...a: unknown[]) => setHandler(...a),
  addNotificationResponseReceivedListener: (...a: unknown[]) =>
    addResponseListener(...a),
  setNotificationChannelAsync: vi.fn(),
  getPermissionsAsync: vi.fn(async () => ({ status: 'denied' })),
  requestPermissionsAsync: vi.fn(async () => ({ status: 'denied' })),
  getExpoPushTokenAsync: vi.fn(async () => ({ data: '' })),
  AndroidImportance: { HIGH: 4 },
}));
vi.mock('expo-device', () => ({ isDevice: false }));
vi.mock('expo-constants', () => ({ default: { expoConfig: { extra: {} } } }));
vi.mock('react-native', () => ({
  Platform: { OS: 'ios' },
  Linking: { openURL: vi.fn(async () => {}) },
}));
vi.mock('../api/client', () => ({
  registerPushToken: vi.fn(async () => {}),
  unregisterPushToken: vi.fn(async () => {}),
}));
vi.mock('../store/config', () => ({ getConfig: () => ({ host: '', token: '' }) }));
vi.mock('../store/nav', () => ({
  navigateTo: vi.fn(),
  navigationRef: { isReady: () => false, navigate: vi.fn() },
}));

describe('initPush — must never break app boot', () => {
  // configureNotificationHandler latches on a module-level flag, so each case
  // needs a fresh module instance to actually reach the native call.
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    addResponseListener.mockReturnValue({ remove: vi.fn() });
    setHandler.mockImplementation(() => {});
  });

  it('returns a detach function on the happy path', async () => {
    const { initPush } = await import('./notifications');
    const detach = initPush();
    expect(typeof detach).toBe('function');
    expect(setHandler).toHaveBeenCalled();
  });

  it('survives the notification handler throwing', async () => {
    setHandler.mockImplementation(() => {
      throw new Error('native module unavailable');
    });
    const { initPush } = await import('./notifications');
    expect(() => initPush()).not.toThrow();
    expect(typeof initPush()).toBe('function');
  });

  it('survives the response listener throwing', async () => {
    addResponseListener.mockImplementation(() => {
      throw new Error('no notification center');
    });
    const { initPush } = await import('./notifications');
    expect(() => initPush()).not.toThrow();
  });
});
