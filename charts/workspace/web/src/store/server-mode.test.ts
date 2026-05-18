import { describe, it, expect, beforeEach, vi } from 'vitest';

// Re-import the module per test so the internal _started latch resets.
async function freshStore() {
  vi.resetModules();
  return await import('./server-mode');
}

describe('server-mode store', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('defaults to read-only so the public demo does not flash mutation UI before the probe lands', async () => {
    const { serverMode, serverModeLoaded } = await freshStore();
    expect(serverMode.value.readOnly).toBe(true);
    expect(serverModeLoaded.value).toBe(false);
  });

  it('flips to the writable shape when /api/mode reports not-readOnly', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ readOnly: false, authed: true, authMode: 'basic' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }),
    ));
    const { serverMode, serverModeLoaded, loadServerMode } = await freshStore();
    await loadServerMode();
    expect(serverMode.value.readOnly).toBe(false);
    expect(serverModeLoaded.value).toBe(true);
  });

  it('falls back to writable when /api/mode is unreachable (dev_server, tests)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const { serverMode, serverModeLoaded, loadServerMode } = await freshStore();
    await loadServerMode();
    // Falls back so a real user isn't stuck behind a phantom readonly gate.
    expect(serverMode.value.readOnly).toBe(false);
    expect(serverModeLoaded.value).toBe(true);
  });

  it('respects readOnly:true from a real public deploy', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ readOnly: true, authed: false, authMode: 'none' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }),
    ));
    const { serverMode, loadServerMode } = await freshStore();
    await loadServerMode();
    expect(serverMode.value.readOnly).toBe(true);
    expect(serverMode.value.authMode).toBe('none');
  });
});
