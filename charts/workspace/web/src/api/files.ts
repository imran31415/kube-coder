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

export async function uploadFile(file: File, destPath: string): Promise<void> {
  // apiRaw handles the Blob body and still propagates the Bearer token +
  // oauth2-proxy session-expired redirect. The previous raw fetch() here
  // silently failed on expired sessions (no /oauth2/start bounce).
  //
  // X-Filename must be ISO-8859-1 per the HTTP header spec — browsers
  // throw TypeError at fetch() time on any Unicode codepoint (smart
  // quotes, emoji, accented letters, CJK). URL-encode at send time;
  // server unquotes via urllib.parse.unquote before use.
  await apiRaw('/api/files/upload', {
    method: 'POST',
    headers: {
      'X-Dest-Path': encodeURIComponent(destPath),
      'X-Filename': encodeURIComponent(file.name),
      'Content-Type': file.type || 'application/octet-stream',
    },
    body: file,
  });
}

export async function makeDirectory(path: string): Promise<void> {
  await apiPost('/api/files/mkdir', { path });
}
