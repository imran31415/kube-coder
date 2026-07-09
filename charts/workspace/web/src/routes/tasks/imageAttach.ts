import { uploadFile } from '../../api/files';

/**
 * Image-paste support for the Send-message composer (issue #179).
 *
 * The composer's follow-up path is text-only: prompts reach Claude Code via
 * `tmux paste-buffer`, which can't carry binary image bytes. But Claude Code
 * reads image files referenced by absolute path in the prompt text. So a
 * pasted/dropped image is uploaded to the task's own attachments dir (reusing
 * the existing /api/files/upload endpoint) and its saved absolute path is
 * injected into the outgoing prompt.
 */

// Claude Code keys an image's MIME type off the file EXTENSION, not magic
// bytes (anthropics/claude-code#35866) — so we must save with an extension
// that matches the blob's real type or the read tool 400s. Map the common
// browser image MIME types; anything unknown falls back to .png.
const MIME_EXT: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/jpg': 'jpg',
  'image/webp': 'webp',
  'image/gif': 'gif',
  'image/bmp': 'bmp',
  'image/svg+xml': 'svg',
  'image/avif': 'avif',
  'image/heic': 'heic',
  'image/heif': 'heif',
  'image/tiff': 'tiff',
};

export function extForImageMime(mime: string): string {
  return MIME_EXT[(mime || '').toLowerCase()] ?? 'png';
}

export function isImageFile(f: File | null | undefined): f is File {
  return !!f && typeof f.type === 'string' && f.type.startsWith('image/');
}

/** Pull image files out of a paste's clipboard items. */
export function imagesFromClipboard(data: DataTransfer | null): File[] {
  if (!data) return [];
  const out: File[] = [];
  for (const item of Array.from(data.items)) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const f = item.getAsFile();
      if (f) out.push(f);
    }
  }
  return out;
}

/**
 * Read text AND images from the async clipboard for the toolbar "Paste" button
 * (which, unlike a textarea paste event, has no ClipboardEvent to inspect).
 * Uses navigator.clipboard.read() where available — that's the only API that
 * exposes image blobs — and falls back to readText() when it's missing or the
 * read is blocked, so behaviour degrades to the old text-only path rather than
 * throwing. Images are wrapped as Files with a MIME-correct extension so the
 * downstream upload names them the same way a direct paste/drop does.
 */
export async function readClipboard(): Promise<{ text: string; images: File[] }> {
  const images: File[] = [];
  let text = '';
  const clip = navigator.clipboard as (Clipboard & { read?: () => Promise<ClipboardItem[]> }) | undefined;
  if (clip?.read) {
    try {
      const items = await clip.read();
      for (const item of items) {
        const imgType = item.types.find((t) => t.startsWith('image/'));
        if (imgType) {
          const blob = await item.getType(imgType);
          images.push(new File([blob], `pasted.${extForImageMime(blob.type)}`, { type: blob.type }));
        } else if (item.types.includes('text/plain')) {
          text += await (await item.getType('text/plain')).text();
        }
      }
      return { text, images };
    } catch {
      /* fall through to readText — read() can throw on permission / focus */
    }
  }
  try {
    text = (await clip?.readText?.()) ?? '';
  } catch {
    /* clipboard unavailable — caller treats empty text + no images as a no-op */
  }
  return { text, images };
}

let _seq = 0;
/**
 * Upload a pasted/dropped image into the task's attachments dir and return the
 * absolute on-disk path Claude Code will read. The filename is generated with
 * a correct extension for the blob's MIME type (see MIME_EXT note above).
 */
export async function uploadTaskImage(taskId: string, file: File): Promise<string> {
  const ext = extForImageMime(file.type);
  const stamp = `${Date.now().toString(36)}-${(_seq++).toString(36)}`;
  const name = `pasted-${stamp}.${ext}`;
  const destPath = `.claude-tasks/${taskId}/attachments`;
  const res = await uploadFile(file, destPath, name);
  return res.absolute_path;
}
