import { beforeEach, describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import { toasts } from './ui';
import { isStorageRefusal, pushUploadError, UPLOAD_REFUSAL_TTL_MS } from './uploadErrors';

const refusal = () =>
  new ApiError('Upload storage is full: raise files.uploadQuota', 507, { code: 'upload_quota_exceeded' });

describe('upload refusals (#556)', () => {
  beforeEach(() => {
    toasts.value = [];
  });

  it('recognises a 507 only', () => {
    expect(isStorageRefusal(refusal())).toBe(true);
    expect(isStorageRefusal(new ApiError('too large', 413, null))).toBe(false);
    expect(isStorageRefusal(new Error('boom'))).toBe(false);
  });

  it('shows the server message and keeps a refusal up long enough to act on', () => {
    pushUploadError(refusal(), 'Upload failed');
    expect(toasts.value[0].message).toContain('files.uploadQuota');
    expect(toasts.value[0].kind).toBe('danger');
    expect(toasts.value[0].ttl).toBe(UPLOAD_REFUSAL_TTL_MS);
  });

  it('leaves ordinary failures on the normal timer', () => {
    pushUploadError(new Error('boom'), 'Upload failed');
    expect(toasts.value[0].message).toBe('boom');
    expect(toasts.value[0].ttl).toBe(3500);
  });

  it('falls back when the rejection is not an Error', () => {
    pushUploadError('nope', 'Upload failed');
    expect(toasts.value[0].message).toBe('Upload failed');
  });
});
