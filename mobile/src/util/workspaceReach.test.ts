/**
 * Telling "the workspace is away" from "the workspace said no" (#728).
 *
 * The case that carries the design is
 * `server.py's own JSON 503 is the workspace talking`: a busy Board or a full
 * task pool answers 503 in JSON, and reading that as "asleep" would put a Wake
 * button over a workspace that is plainly up.
 */
import { describe, expect, it, vi } from 'vitest';
import type { ControllerWorkspace } from '../api/types';
import {
  NO_WAKE_PATH,
  SLEEPING,
  UNREACHABLE,
  bannerAction,
  classifyDown,
  isDownError,
  matchWorkspace,
  pollDelay,
  reachFromController,
  wakeWorkspace,
  type WakeDeps,
} from './workspaceReach';

function ws(over: Partial<ControllerWorkspace>): ControllerWorkspace {
  return {
    user: 'raman',
    deployment: 'ws-raman',
    namespace: 'ws-raman',
    isolated: true,
    state: 'running',
    desiredReplicas: 1,
    readyReplicas: 1,
    url: 'https://raman.dev.example.io/',
    detail: '1/1 ready',
    version: null,
    updateAvailable: false,
    ...over,
  };
}

describe('classifyDown', () => {
  it('reads the ingress HTML 502/503/504 as unreachable', () => {
    for (const status of [502, 503, 504]) {
      expect(classifyDown(status, false, '<html>503 Service Temporarily Unavailable</html>')).toBe(UNREACHABLE);
    }
  });

  it("server.py's own JSON 503 is the workspace talking", () => {
    expect(classifyDown(503, true, { error: 'board subsystem unavailable' })).toBeNull();
  });

  it("reads the keeper's JSON as asleep, by code or by error", () => {
    expect(classifyDown(503, true, { code: SLEEPING, error: 'asleep' })).toBe(SLEEPING);
    expect(classifyDown(503, true, { error: SLEEPING })).toBe(SLEEPING);
  });

  it('other HTML errors are not about reachability', () => {
    expect(classifyDown(404, false, 'Not Found')).toBeNull();
    expect(classifyDown(500, false, 'Internal Server Error')).toBeNull();
  });

  it('survives a JSON body that failed to parse', () => {
    expect(classifyDown(503, true, null)).toBeNull();
  });
});

describe('isDownError', () => {
  it('matches only the two reachability codes', () => {
    expect(isDownError({ code: SLEEPING })).toBe(true);
    expect(isDownError({ code: UNREACHABLE })).toBe(true);
    expect(isDownError({ code: 'live' })).toBe(false);
    expect(isDownError(new Error('boom'))).toBe(false);
    expect(isDownError(undefined)).toBe(false);
  });
});

describe('pollDelay', () => {
  it('starts quick and settles at the slowest step', () => {
    expect([0, 1, 2, 3, 50].map(pollDelay)).toEqual([2000, 5000, 10000, 10000, 10000]);
    expect(pollDelay(-1)).toBe(2000);
  });
});

describe('matchWorkspace', () => {
  const list = [ws({ user: 'imran', url: 'https://imran.dev.example.io/' }), ws({})];

  it('matches on hostname, ignoring scheme, port and path', () => {
    expect(matchWorkspace('https://raman.dev.example.io', list)?.user).toBe('raman');
    expect(matchWorkspace('https://RAMAN.dev.example.io:443/x', list)?.user).toBe('raman');
  });

  it('falls back to the user slug when the controller has no domain', () => {
    const noDomain = [ws({ url: null })];
    expect(matchWorkspace('https://raman.dev.example.io', noDomain)?.user).toBe('raman');
  });

  it('never guesses by slug when the controller does publish URLs', () => {
    expect(matchWorkspace('https://raman.other.io', list)).toBeUndefined();
  });

  it('returns nothing for a host it cannot parse', () => {
    expect(matchWorkspace('not a url', list)).toBeUndefined();
  });
});

describe('reachFromController', () => {
  it('scaled to 0 is asleep; scaled up but not ready is waking', () => {
    expect(reachFromController(ws({ desiredReplicas: 0, readyReplicas: 0 }))).toBe('asleep');
    expect(reachFromController(ws({ desiredReplicas: 1, readyReplicas: 0 }))).toBe('waking');
    expect(reachFromController(ws({}))).toBe('awake');
  });

  // #728 step 5: the controller tells sleeping from stopped.
  it('a workspace an admin stopped is stopped, not asleep', () => {
    const stopped = ws({ state: 'stopped', desiredReplicas: 0, readyReplicas: 0, sleepAware: true });
    expect(reachFromController(stopped)).toBe('stopped');
  });

  it('a workspace its keeper put to sleep is still asleep', () => {
    const sleeping = ws({ state: 'sleeping', desiredReplicas: 0, readyReplicas: 0, sleepAware: true });
    expect(reachFromController(sleeping)).toBe('asleep');
  });

  it("does not believe an older controller's stopped, which it says of every workspace at 0", () => {
    // No sleepAware at all: a controller from before step 5. Calling this
    // stopped would take the Wake button off a workspace a tap might wake.
    const old = ws({ state: 'stopped', desiredReplicas: 0, readyReplicas: 0 });
    expect('sleepAware' in old).toBe(false);
    expect(reachFromController(old)).toBe('asleep');
  });
});

describe('bannerAction', () => {
  it('offers a wake while it is asleep or cannot be reached', () => {
    expect(bannerAction('asleep', false)).toBe('wake');
    expect(bannerAction('unreachable', false)).toBe('wake');
    expect(bannerAction('asleep', true)).toBe('wake');
  });

  it("offers the controller's Start for a stopped workspace, and nothing without one", () => {
    expect(bannerAction('stopped', true)).toBe('start');
    expect(bannerAction('stopped', false)).toBeNull();
  });

  it('offers nothing while it is waking or awake', () => {
    expect(bannerAction('waking', true)).toBeNull();
    expect(bannerAction('awake', true)).toBeNull();
  });
});

describe('wakeWorkspace', () => {
  function deps(over: Partial<WakeDeps> = {}): WakeDeps {
    return {
      host: 'https://raman.dev.example.io',
      viaKeeper: async () => false,
      controller: { list: async () => [ws({})], start: vi.fn(async () => {}) },
      ...over,
    };
  }

  it('uses the keeper first when there is one', async () => {
    const d = deps({ viaKeeper: async () => true });
    expect(await wakeWorkspace(d)).toEqual({ ok: true, via: 'keeper' });
    expect(d.controller?.start).not.toHaveBeenCalled();
  });

  it("falls back to the controller's Start for this workspace", async () => {
    const d = deps();
    expect(await wakeWorkspace(d)).toEqual({ ok: true, via: 'controller' });
    expect(d.controller?.start).toHaveBeenCalledWith('raman');
  });

  it('says plainly when the phone has no way to wake it', async () => {
    expect(await wakeWorkspace(deps({ controller: null }))).toEqual({ ok: false, message: NO_WAKE_PATH });
  });

  it('refuses to start a workspace the controller does not list', async () => {
    const d = deps({ controller: { list: async () => [], start: vi.fn(async () => {}) } });
    expect((await wakeWorkspace(d)).ok).toBe(false);
    expect(d.controller?.start).not.toHaveBeenCalled();
  });

  it("marks the keeper's refusal for a stopped workspace, so the banner stops offering a wake", async () => {
    const refusal = Object.assign(
      new Error('Your workspace is stopped, not asleep. Start it from the dashboard or your controller.'),
      { status: 409 },
    );
    const d = deps({
      viaKeeper: async () => {
        throw refusal;
      },
    });
    expect(await wakeWorkspace(d)).toEqual({ ok: false, message: refusal.message, stopped: true });
    // Refused by the keeper is final for this tap: nothing goes to the controller.
    expect(d.controller?.start).not.toHaveBeenCalled();
  });

  it('any other refusal is just a message', async () => {
    const forbidden = Object.assign(new Error("This app isn't allowed to wake this workspace."), { status: 403 });
    const d = deps({
      viaKeeper: async () => {
        throw forbidden;
      },
    });
    expect(await wakeWorkspace(d)).toEqual({ ok: false, message: forbidden.message });
  });

  it('turns a failure into a message instead of throwing', async () => {
    const d = deps({
      controller: {
        list: async () => {
          throw new Error('unauthorized');
        },
        start: vi.fn(async () => {}),
      },
    });
    expect(await wakeWorkspace(d)).toEqual({ ok: false, message: 'unauthorized' });
  });
});
