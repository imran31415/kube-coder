import { apiGet, apiPost, apiRaw, api, withOauthPrefix } from './client';

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

/** Preview descriptor returned by GET /api/files/preview. Text files carry
 *  their (size-capped) content; images/video signal an inline render via the
 *  raw endpoint; binary/undecodable content signals "download instead". */
export type FilePreview =
  | { kind: 'text'; path: string; mime: string; size: number; content: string; truncated: boolean }
  | { kind: 'image'; path: string; mime: string; size: number }
  | { kind: 'video'; path: string; mime: string; size: number }
  | { kind: 'binary'; path: string; mime: string; size: number; reason?: string };

export const previewFile = (path: string) => apiGet<FilePreview>('/api/files/preview', { path });

/** Same-origin URL for streaming raw media bytes (image/video preview). Auth
 *  rides the oauth2 session cookie the SPA was loaded with (see client.ts). */
export const fileRawUrl = (path: string) =>
  `${withOauthPrefix('/api/files/raw')}?path=${encodeURIComponent(path)}`;

/**
 * Download a file via an authenticated fetch → Blob → object-URL anchor. Going
 * through apiRaw (rather than a bare <a href>) keeps the Bearer token + the
 * oauth session-expired redirect behaviour, and works whether auth is a cookie
 * or a dev token.
 */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const res = await apiRaw(`/api/files/download?path=${encodeURIComponent(path)}`, { method: 'GET' });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke on the next tick so the click has consumed the URL.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function deleteFile(path: string): Promise<void> {
  await api(`/api/files?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
}

/** Move/rename `from` → `to` (both relative to /home/dev). Returns the new
 *  path the server settled on. */
export async function renameFile(from: string, to: string): Promise<string> {
  const r = await apiPost<{ ok: boolean; path: string }>('/api/files/rename', { from, to });
  return r.path;
}

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
