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
const navigateTo = vi.fn();
const navigate = vi.fn();
let navReady = false;
vi.mock('../store/nav', () => ({
  navigateTo: (...a: unknown[]) => navigateTo(...a),
  navigationRef: { isReady: () => navReady, navigate: (...a: unknown[]) => navigate(...a) },
}));

describe('initPush — must never break app boot', () => {
  // configureNotificationHandler latches on a module-level flag, so each case
  // needs a fresh module instance to actually reach the native call.
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    addResponseListener.mockReturnValue({ remove: vi.fn() });
    setHandler.mockImplementation(() => {});
    navReady = false;
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

/**
 * Where a tapped notification lands (#692).
 *
 * The server has always pushed board review items — push_notify copies the
 * feed link's ref into `data.ref`, and a board review emits
 * "board:<board_id>:<item_id>". Mobile could not parse three parts, so every
 * one of those taps fell through to the default and opened the Feed.
 */
describe('handleNotificationTap — board review pushes', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    vi.resetModules();
    navReady = true;
    const { _resetBoardFocusForTest } = await import('../store/boardFocus');
    _resetBoardFocusForTest();
  });

  it('opens Board and parks the item for the screen to focus', async () => {
    const { handleNotificationTap } = await import('./notifications');
    const { peekBoardFocus } = await import('../store/boardFocus');
    handleNotificationTap({ ref: 'board:acme-jira:812', waiting: true });
    expect(navigateTo).toHaveBeenCalledWith('Board');
    expect(peekBoardFocus()).toMatchObject({ boardId: 'acme-jira', itemId: '812' });
  });

  it('parks a colon-bearing item id unchanged', async () => {
    const { handleNotificationTap } = await import('./notifications');
    const { peekBoardFocus } = await import('../store/boardFocus');
    handleNotificationTap({ ref: 'board:kube-coder-gh:I_kwDOA:4102' });
    expect(peekBoardFocus()?.itemId).toBe('I_kwDOA:4102');
  });

  it('still falls back to the Feed for a malformed board ref', async () => {
    const { handleNotificationTap } = await import('./notifications');
    const { peekBoardFocus } = await import('../store/boardFocus');
    handleNotificationTap({ ref: 'board:acme-jira' });
    expect(navigateTo).toHaveBeenCalledWith('Feed');
    expect(peekBoardFocus()).toBeNull();
  });

  it('does nothing at all before the navigator is ready', async () => {
    navReady = false;
    const { handleNotificationTap } = await import('./notifications');
    const { peekBoardFocus } = await import('../store/boardFocus');
    handleNotificationTap({ ref: 'board:acme-jira:812' });
    expect(navigateTo).not.toHaveBeenCalled();
    expect(peekBoardFocus()).toBeNull();
  });
});
