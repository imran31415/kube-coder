import { apiGet, withOauthPrefix } from './client';

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
  const res = await fetch(withOauthPrefix('/api/files/upload'), {
    method: 'POST',
    headers: {
      'X-Dest-Path': destPath,
      'X-Filename': file.name,
      'Content-Type': file.type || 'application/octet-stream',
    },
    body: file,
  });
  if (!res.ok) {
    throw new Error(`Upload failed: ${res.status} ${res.statusText}`);
  }
}

export async function makeDirectory(path: string): Promise<void> {
  const res = await fetch(withOauthPrefix('/api/files/mkdir'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) throw new Error(`mkdir failed: ${res.status}`);
}
