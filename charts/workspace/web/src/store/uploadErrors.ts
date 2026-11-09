import { ApiError } from '../api/client';
import { pushToast } from './ui';

/** Long enough to read a refusal that names what is full and what to change. */
export const UPLOAD_REFUSAL_TTL_MS = 15000;

/** True when the server refused an upload for lack of room (#556) — quota or
 *  free-space floor. Its message is what the user has to act on. */
export function isStorageRefusal(err: unknown): err is ApiError {
  return err instanceof ApiError && err.status === 507;
}

/** Toast a failed upload, keeping a storage refusal up long enough to act on. */
export function pushUploadError(err: unknown, fallback: string): void {
  pushToast(err instanceof Error ? err.message : fallback, {
    kind: 'danger',
    ...(isStorageRefusal(err) ? { ttl: UPLOAD_REFUSAL_TTL_MS } : {}),
  });
}
