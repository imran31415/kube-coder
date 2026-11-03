/**
 * Desktop detection (#833) is the presence of Tauri's injected bridge, so the
 * same web export is a plain browser app without it.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { invoke, isDesktop, listen } from './bridge';

afterEach(() => vi.unstubAllGlobals());

describe('bridge', () => {
  it('is not the desktop app without the bridge', async () => {
    expect(isDesktop()).toBe(false);
    await expect(invoke('kc_live_status')).rejects.toThrow(/outside the desktop app/);
    const off = await listen('kc://live', () => {});
    expect(typeof off).toBe('function');
  });

  it('calls through the injected bridge when present', async () => {
    const core = { invoke: vi.fn(async () => 'ok') };
    const unlisten = vi.fn();
    const event = {
      listen: vi.fn(async (_e: string, cb: (e: { payload: number }) => void) => {
        cb({ payload: 7 });
        return unlisten;
      }),
    };
    vi.stubGlobal('__TAURI__', { core, event });
    expect(isDesktop()).toBe(true);
    expect(await invoke('kc_live_status', { a: 1 })).toBe('ok');
    expect(core.invoke).toHaveBeenCalledWith('kc_live_status', { a: 1 });
    const got: number[] = [];
    const off = await listen<number>('kc://badge', (p) => got.push(p));
    expect(got).toEqual([7]);
    expect(off).toBe(unlisten);
  });
});
