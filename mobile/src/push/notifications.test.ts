/**
 * The push glue: boot safety, where a tap lands, the Settings switch and
 * tap-marks-read (#685).
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
const getPermissions = vi.fn();
const requestPermissions = vi.fn();
const getExpoToken = vi.fn();
const device = { isDevice: false };

vi.mock('expo-notifications', () => ({
  setNotificationHandler: (...a: unknown[]) => setHandler(...a),
  addNotificationResponseReceivedListener: (...a: unknown[]) =>
    addResponseListener(...a),
  setNotificationChannelAsync: vi.fn(),
  getPermissionsAsync: (...a: unknown[]) => getPermissions(...a),
  requestPermissionsAsync: (...a: unknown[]) => requestPermissions(...a),
  getExpoPushTokenAsync: (...a: unknown[]) => getExpoToken(...a),
  AndroidImportance: { HIGH: 4 },
}));
// A getter: the mocked module is cached across vi.resetModules, so a plain
// value would freeze at whatever the first import saw.
vi.mock('expo-device', () => ({
  get isDevice() {
    return device.isDevice;
  },
}));
vi.mock('expo-constants', () => ({ default: { expoConfig: { extra: {} } } }));
vi.mock('react-native', () => ({
  Platform: { OS: 'ios' },
  Linking: { openURL: vi.fn(async () => {}) },
}));
const registerPushToken = vi.fn();
const unregisterPushToken = vi.fn();
const markFeedRead = vi.fn();
vi.mock('../api/client', () => ({
  registerPushToken: (...a: unknown[]) => registerPushToken(...a),
  unregisterPushToken: (...a: unknown[]) => unregisterPushToken(...a),
  markFeedRead: (...a: unknown[]) => markFeedRead(...a),
}));

interface FakeConfig {
  host: string;
  token: string;
  pushEnabled: boolean;
  loaded: boolean;
}
let cfg: FakeConfig = { host: '', token: '', pushEnabled: true, loaded: true };
const cfgListeners = new Set<(c: FakeConfig) => void>();
const setPushEnabled = vi.fn();
vi.mock('../store/config', () => ({
  getConfig: () => cfg,
  setPushEnabled: (on: boolean) => setPushEnabled(on),
  subscribe: (l: (c: FakeConfig) => void) => {
    cfgListeners.add(l);
    return () => cfgListeners.delete(l);
  },
}));

// Every case starts from the same inert world: not a device, not connected,
// push on, no permission, no token, and a server that accepts everything.
beforeEach(() => {
  device.isDevice = false;
  cfg = { host: '', token: '', pushEnabled: true, loaded: true };
  cfgListeners.clear();
  getPermissions.mockReset().mockImplementation(async () => ({ status: 'denied' }));
  requestPermissions.mockReset().mockImplementation(async () => ({ status: 'denied' }));
  getExpoToken.mockReset().mockImplementation(async () => ({ data: '' }));
  registerPushToken.mockReset().mockImplementation(async () => {});
  unregisterPushToken.mockReset().mockImplementation(async () => {});
  markFeedRead.mockReset().mockImplementation(async () => {});
  setPushEnabled.mockReset().mockImplementation(async (on: boolean) => {
    cfg = { ...cfg, pushEnabled: on };
  });
});

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

const PHONE = 'ExponentPushToken[phone]';
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/**
 * Settings → Notifications (#685). Before it, disconnecting the workspace was
 * the only way to stop pushes from the phone.
 */
describe('the Notifications switch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    device.isDevice = true;
    cfg = { host: 'https://ws.example', token: 'api-token', pushEnabled: true, loaded: true };
    getPermissions.mockImplementation(async () => ({ status: 'granted' }));
    getExpoToken.mockImplementation(async () => ({ data: PHONE }));
  });

  it('registers the phone while on', async () => {
    const { registerForPush } = await import('./notifications');
    await registerForPush();
    expect(registerPushToken).toHaveBeenCalledWith(PHONE, 'ios');
  });

  it('does nothing at all while off — not even a permission prompt', async () => {
    cfg.pushEnabled = false;
    const { registerForPush } = await import('./notifications');
    await registerForPush();
    expect(getPermissions).not.toHaveBeenCalled();
    expect(requestPermissions).not.toHaveBeenCalled();
    expect(getExpoToken).not.toHaveBeenCalled();
    expect(registerPushToken).not.toHaveBeenCalled();
  });

  it('never registers when switched off while the token was being fetched', async () => {
    getExpoToken.mockImplementation(async () => {
      cfg = { ...cfg, pushEnabled: false };
      return { data: PHONE };
    });
    const { registerForPush } = await import('./notifications');
    await registerForPush();
    expect(registerPushToken).not.toHaveBeenCalled();
  });

  it('undoes a registration the switch overtook mid-flight', async () => {
    registerPushToken.mockImplementation(async () => {
      cfg = { ...cfg, pushEnabled: false };
    });
    const { registerForPush } = await import('./notifications');
    await registerForPush();
    expect(unregisterPushToken).toHaveBeenCalledWith(PHONE);
  });

  it('off drops the token registered this launch; on registers again', async () => {
    const push = await import('./notifications');
    await push.registerForPush();
    getExpoToken.mockClear();

    await push.setPushNotificationsEnabled(false);
    expect(setPushEnabled).toHaveBeenLastCalledWith(false);
    expect(unregisterPushToken).toHaveBeenCalledWith(PHONE);
    expect(getExpoToken).not.toHaveBeenCalled(); // used the token it remembered

    await push.setPushNotificationsEnabled(true);
    expect(setPushEnabled).toHaveBeenLastCalledWith(true);
    expect(registerPushToken).toHaveBeenCalledTimes(2);
  });

  it('off on a launch that never registered still reaches the workspace', async () => {
    // lastRegistered lives in memory, so it is empty on this fresh module even
    // though the workspace holds the token from an earlier launch.
    const push = await import('./notifications');
    await push.setPushNotificationsEnabled(false);
    expect(requestPermissions).not.toHaveBeenCalled();
    expect(getExpoToken).toHaveBeenCalled();
    expect(unregisterPushToken).toHaveBeenCalledWith(PHONE);
  });

  it('off without notification permission neither prompts nor fails', async () => {
    getPermissions.mockImplementation(async () => ({ status: 'denied' }));
    const push = await import('./notifications');
    await expect(push.setPushNotificationsEnabled(false)).resolves.toBeUndefined();
    expect(requestPermissions).not.toHaveBeenCalled();
    expect(getExpoToken).not.toHaveBeenCalled();
    expect(unregisterPushToken).not.toHaveBeenCalled();
    expect(cfg.pushEnabled).toBe(false);
  });

  it('off holds when the workspace is unreachable, and the next launch stays quiet', async () => {
    unregisterPushToken.mockImplementation(async () => {
      throw new Error('network down');
    });
    const push = await import('./notifications');
    await expect(push.setPushNotificationsEnabled(false)).resolves.toBeUndefined();
    expect(cfg.pushEnabled).toBe(false);

    vi.resetModules();
    const nextLaunch = await import('./notifications');
    await nextLaunch.registerForPush();
    expect(registerPushToken).not.toHaveBeenCalled();
  });

  it('on without permission saves the choice and does not throw', async () => {
    cfg.pushEnabled = false;
    getPermissions.mockImplementation(async () => ({ status: 'denied' }));
    const push = await import('./notifications');
    await expect(push.setPushNotificationsEnabled(true)).resolves.toBeUndefined();
    expect(cfg.pushEnabled).toBe(true);
    expect(requestPermissions).toHaveBeenCalled();
    expect(registerPushToken).not.toHaveBeenCalled();
  });
});

/**
 * A tap reads the alert (#685). The server re-sends a repeat of an alert only
 * once its Feed row has been read, and until now a tap never marked it — so
 * acting on a push would have silenced every later repeat of that alert.
 */
describe('handleNotificationTap — a tap marks the alert read', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    navReady = true;
    cfg = { host: 'https://ws.example', token: 'api-token', pushEnabled: true, loaded: true };
  });

  it('marks the row read and still navigates', async () => {
    const { handleNotificationTap } = await import('./notifications');
    handleNotificationTap({ ref: 'task:t1', feedId: 'fd_1_abc', waiting: true });
    await flush();
    expect(markFeedRead).toHaveBeenCalledWith('fd_1_abc');
    expect(navigate).toHaveBeenCalled();
  });

  it('marks it read even before the navigator is ready', async () => {
    navReady = false;
    const { handleNotificationTap } = await import('./notifications');
    handleNotificationTap({ ref: 'task:t1', feedId: 'fd_2' });
    await flush();
    expect(markFeedRead).toHaveBeenCalledWith('fd_2');
    expect(navigate).not.toHaveBeenCalled();
  });

  it('on a cold start, waits for the saved connection before marking', async () => {
    cfg = { ...cfg, host: '', token: '', loaded: false };
    const { handleNotificationTap } = await import('./notifications');
    handleNotificationTap({ feedId: 'fd_cold' });
    await flush();
    expect(markFeedRead).not.toHaveBeenCalled();

    cfg = { ...cfg, host: 'https://ws.example', token: 'api-token', loaded: true };
    for (const listener of [...cfgListeners]) listener(cfg);
    await flush();
    expect(markFeedRead).toHaveBeenCalledWith('fd_cold');
    expect(cfgListeners.size).toBe(0); // unsubscribed after the one send
  });

  it('a tap without a feed id marks nothing', async () => {
    const { handleNotificationTap } = await import('./notifications');
    handleNotificationTap({ ref: 'task:t1' });
    handleNotificationTap({ ref: 'task:t1', feedId: '   ' });
    handleNotificationTap(undefined);
    await flush();
    expect(markFeedRead).not.toHaveBeenCalled();
  });

  it('a failing mark-read never blocks navigation or escapes', async () => {
    markFeedRead.mockImplementation(async () => {
      throw new Error('network down');
    });
    const { handleNotificationTap } = await import('./notifications');
    expect(() => handleNotificationTap({ ref: 'task:t9', feedId: 'fd_9' })).not.toThrow();
    markFeedRead.mockImplementation(() => {
      throw new Error('thrown synchronously');
    });
    expect(() => handleNotificationTap({ ref: 'task:t9', feedId: 'fd_10' })).not.toThrow();
    await flush();
    expect(navigate).toHaveBeenCalledTimes(2);
  });
});
