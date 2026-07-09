import { apiGet, apiPost, apiRaw } from './client';

export interface FileEntry {
  name: string;
  kind: 'dir' | 'file';
  size: number;
  mtime: number;
}
export interface FileListing {
  path: string;
  entries: FileEntry[];
}

export const listFiles = (path = '') => apiGet<FileListing>('/api/files/list', { path });

export interface UploadResult {
  ok: boolean;
  /** Path relative to /home/dev. */
  path: string;
  /** Absolute on-disk path, e.g. /home/dev/uploads/…/foo.png. */
  absolute_path: string;
  size: number;
}

/**
 * Upload a file/blob into `destPath` (relative to /home/dev) and return the
 * server's saved-path result. Pass `filename` to override the stored name —
 * required for clipboard blobs, which have no meaningful `.name`.
 */
export async function uploadFile(
  file: File | Blob,
  destPath: string,
  filename?: string,
): Promise<UploadResult> {
  const name = filename ?? (file instanceof File ? file.name : 'upload.bin');
  // apiRaw handles the Blob body and still propagates the Bearer token +
  // oauth2-proxy session-expired redirect. The previous raw fetch() here
  // silently failed on expired sessions (no /oauth2/start bounce).
  //
  // X-Filename must be ISO-8859-1 per the HTTP header spec — browsers
  // throw TypeError at fetch() time on any Unicode codepoint (smart
  // quotes, emoji, accented letters, CJK). URL-encode at send time;
  // server unquotes via urllib.parse.unquote before use.
  const res = await apiRaw('/api/files/upload', {
    method: 'POST',
    headers: {
      'X-Dest-Path': encodeURIComponent(destPath),
      'X-Filename': encodeURIComponent(name),
      'Content-Type': file.type || 'application/octet-stream',
    },
    body: file,
  });
  return (await res.json()) as UploadResult;
}

export async function makeDirectory(path: string): Promise<void> {
  await apiPost('/api/files/mkdir', { path });
}
