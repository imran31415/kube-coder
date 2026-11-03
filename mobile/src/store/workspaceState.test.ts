/**
 * The app-wide reachability answer (#728): which reports may move it, and the
 * one-shot "when it is back" hook the push and queue paths rely on.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SLEEPING, UNREACHABLE } from '../util/workspaceReach';
import {
  getWorkspaceState,
  onceAwake,
  reportAwake,
  reportDown,
  setReach,
  subscribeWorkspaceState,
} from './workspaceState';

beforeEach(() => reportAwake());

describe('workspaceState', () => {
  it('an unexplained failure is "unreachable"; the keeper saying so is "asleep"', () => {
    reportDown(UNREACHABLE);
    expect(getWorkspaceState().reach).toBe('unreachable');
    reportDown(SLEEPING);
    expect(getWorkspaceState().reach).toBe('asleep');
  });

  it('never downgrades "asleep" to "unreachable"', () => {
    setReach('asleep');
    reportDown(UNREACHABLE);
    expect(getWorkspaceState().reach).toBe('asleep');
  });

  it("keeps \"stopped\" through the keeper's away answer, which says the same for asleep", () => {
    // Otherwise the next failed request would turn it back into "asleep" and
    // put up a Wake button the keeper is bound to refuse.
    setReach('stopped');
    reportDown(SLEEPING);
    reportDown(UNREACHABLE);
    expect(getWorkspaceState().reach).toBe('stopped');
    // ...and an admin's Start is noticed like any other recovery.
    reportAwake();
    expect(getWorkspaceState().reach).toBe('awake');
  });

  it('a wake in flight ignores the failures it expects', () => {
    setReach('waking');
    reportDown(UNREACHABLE);
    reportDown(SLEEPING);
    expect(getWorkspaceState().reach).toBe('waking');
    reportAwake();
    expect(getWorkspaceState().reach).toBe('awake');
  });

  it('notifies on a change only', () => {
    const seen: string[] = [];
    const off = subscribeWorkspaceState((s) => seen.push(s.reach));
    reportAwake();
    reportDown(UNREACHABLE);
    reportDown(UNREACHABLE);
    reportAwake();
    off();
    reportDown(UNREACHABLE);
    expect(seen).toEqual(['unreachable', 'awake']);
  });

  it('onceAwake runs at once when awake, else once on waking', () => {
    const now = vi.fn();
    onceAwake(now);
    expect(now).toHaveBeenCalledTimes(1);

    const later = vi.fn();
    reportDown(UNREACHABLE);
    onceAwake(later);
    expect(later).not.toHaveBeenCalled();
    reportAwake();
    reportDown(UNREACHABLE);
    reportAwake();
    expect(later).toHaveBeenCalledTimes(1);
  });

  it('a cancelled onceAwake never runs', () => {
    const fn = vi.fn();
    reportDown(UNREACHABLE);
    onceAwake(fn)();
    reportAwake();
    expect(fn).not.toHaveBeenCalled();
  });
});
