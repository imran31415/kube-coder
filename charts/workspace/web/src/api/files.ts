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

/** Same-origin URL for viewing a document inline (PDF/HTML/SVG) in a sandboxed
 *  <iframe>. The server sends `Content-Security-Policy: sandbox` for HTML/SVG so
 *  it can't touch the dashboard origin. Auth rides the oauth2 session cookie. */
export const fileViewUrl = (path: string) =>
  `${withOauthPrefix('/api/files/view')}?path=${encodeURIComponent(path)}`;

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

async function postUpload(
  file: File | Blob,
  destPath: string,
  name: string,
  extract: boolean,
): Promise<Response> {
  // apiRaw handles the Blob body and still propagates the Bearer token +
  // oauth2-proxy session-expired redirect. The previous raw fetch() here
  // silently failed on expired sessions (no /oauth2/start bounce).
  //
  // X-Filename must be ISO-8859-1 per the HTTP header spec — browsers
  // throw TypeError at fetch() time on any Unicode codepoint (smart
  // quotes, emoji, accented letters, CJK). URL-encode at send time;
  // server unquotes via urllib.parse.unquote before use.
  const headers: Record<string, string> = {
    'X-Dest-Path': encodeURIComponent(destPath),
    'X-Filename': encodeURIComponent(name),
    'Content-Type': file.type || 'application/octet-stream',
  };
  if (extract) headers['X-Extract'] = 'zip';
  return apiRaw('/api/files/upload', { method: 'POST', headers, body: file });
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
  const res = await postUpload(file, destPath, name, false);
  return (await res.json()) as UploadResult;
}

export interface ExtractResult {
  ok: boolean;
  /** Destination directory, relative to /home/dev. */
  path: string;
  /** Absolute destination directory the archive was unpacked into. */
  absolute_path: string;
  /** Number of files written. */
  extracted: number;
}

/**
 * Upload a .zip and have the server unpack it into `destPath` (relative to
 * /home/dev). The archive itself is not kept — only the extracted tree.
 */
export async function uploadZip(file: File | Blob, destPath: string, filename?: string): Promise<ExtractResult> {
  const name = filename ?? (file instanceof File ? file.name : 'upload.zip');
  const res = await postUpload(file, destPath, name, true);
  return (await res.json()) as ExtractResult;
}

export interface UploadBatchItem {
  file: File;
  /** Destination dir relative to /home/dev (may differ per item — folder
   *  uploads preserve each file's relative subdirectory). */
  destPath: string;
  filename?: string;
}

export interface UploadBatchResult {
  done: number;
  failed: Array<{ name: string; error: string }>;
}

/**
 * Upload many files with bounded parallelism (issue #356). One slow/huge file
 * doesn't serialize the rest, and a hundred small ones don't open a hundred
 * concurrent requests. Failures are collected per-file rather than aborting
 * the batch; `onProgress(finished, total)` fires after every settle.
 */
export async function uploadBatch(
  items: UploadBatchItem[],
  onProgress?: (finished: number, total: number) => void,
  concurrency = 4,
): Promise<UploadBatchResult> {
  const failed: UploadBatchResult['failed'] = [];
  let done = 0;
  let finished = 0;
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const item = items[next++];
      try {
        await uploadFile(item.file, item.destPath, item.filename);
        done++;
      } catch (err) {
        failed.push({
          name: item.filename ?? item.file.name,
          error: err instanceof Error ? err.message : String(err),
        });
      }
      finished++;
      onProgress?.(finished, items.length);
    }
  }
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, worker);
  await Promise.all(workers);
  return { done, failed };
}

export async function makeDirectory(path: string): Promise<void> {
  await apiPost('/api/files/mkdir', { path });
}
