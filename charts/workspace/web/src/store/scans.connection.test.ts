import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  connection,
  connectionResult,
  loadConnection,
  modelSettingsOpen,
  revealModelSettings,
  startScan,
} from './scans';
import { toasts } from './ui';
import { ApiError } from '../api/client';
import * as api from '../api/scans';
import type { ConnectionView } from '../api/scans';

function view(state: ConnectionView['install']['state']): ConnectionView {
  return {
    model: 'prov/model',
    api_base: '',
    has_key: true,
    uses_subscription: false,
    subscription: { signed_in: false },
    configured: true,
    install: { state, version: '1', error: '' },
    backend: { ok: true, reason: '', detail: '' },
  };
}

const REFUSED = 'The scanner does not recognise this model name.';

describe('a scan refused for its model name (#886)', () => {
  beforeEach(() => {
    toasts.value = [];
    modelSettingsOpen.value = false;
    revealModelSettings.value = false;
  });
  afterEach(() => vi.restoreAllMocks());

  it('says why and opens Model settings, where the fix is', async () => {
    vi.spyOn(api, 'createScan').mockRejectedValue(
      new ApiError(REFUSED, 409, { error: REFUSED, code: 'model_invalid' }),
    );
    expect(await startScan({ port: 3000, mode: 'quick' })).toBeNull();
    expect(toasts.value.map((t) => t.message)).toEqual([REFUSED]);
    expect(modelSettingsOpen.value).toBe(true);
    expect(revealModelSettings.value).toBe(true);
  });

  it('leaves the settings alone for any other refusal', async () => {
    vi.spyOn(api, 'createScan').mockRejectedValue(
      new ApiError('Nothing is running on port 3000.', 400, { error: 'x' }),
    );
    await startScan({ port: 3000, mode: 'quick' });
    expect(modelSettingsOpen.value).toBe(false);
  });
});

describe('the first-use install', () => {
  beforeEach(() => {
    connection.value = null;
    connectionResult.value = null;
  });
  afterEach(() => vi.restoreAllMocks());

  it('checks the model the moment the install lands', async () => {
    // Saving mid-install can only be told "still being installed", so the
    // real check would otherwise never run before the first scan.
    const check = vi
      .spyOn(api, 'testScanConnection')
      .mockResolvedValue({ ok: false, detail: REFUSED });
    vi.spyOn(api, 'getScanConnection')
      .mockResolvedValueOnce(view('installing'))
      .mockResolvedValueOnce(view('ready'))
      .mockResolvedValueOnce(view('ready'));
    await loadConnection();
    expect(check).not.toHaveBeenCalled();
    await loadConnection();
    await vi.waitFor(() => expect(connectionResult.value).toEqual({ ok: false, detail: REFUSED }));
    await loadConnection();
    expect(check).toHaveBeenCalledTimes(1);
  });
});
