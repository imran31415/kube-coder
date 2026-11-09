/**
 * A storage refusal (#556) has to reach the screen as the server's own words:
 * the message names what is full and which setting to change.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The real config store pulls in React Native, which can't load under node.
vi.mock('react-native', () => ({ Platform: { OS: 'ios' } }));
vi.mock('../store/config', () => ({
  getConfig: () => ({ host: 'https://me.example.com', token: 't', mock: false }),
}));

import { ApiError, uploadTaskImage } from './client';

const MSG = 'Upload storage is full: raise files.uploadQuota in the chart values.';

describe('uploadTaskImage storage refusal', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) =>
      String(url).startsWith('file:')
        ? { blob: async () => new Blob(['x']) }
        : {
            ok: false,
            status: 507,
            json: async () => ({ error: MSG, code: 'upload_quota_exceeded' }),
          },
    ));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("throws the server's message and the 507", async () => {
    const err = await uploadTaskImage('t1', { uri: 'file:///a.jpg', mimeType: 'image/jpeg' }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe(MSG);
    expect(err.status).toBe(507);
  });
});
