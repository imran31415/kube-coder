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

// ── Non-image attachments (issue #293) ─────────────────────────────────────
// The Hypervisor chat can attach any file Claude Code can actually *read* by
// path (docs, text, source code, images). Everything is path-based: the file
// is uploaded to disk and its absolute path is appended to the prompt, so the
// on-disk EXTENSION is what Claude keys the file type off — see MIME_EXT note.
//
// Video is deliberately excluded: Claude can't read video, so silently
// appending a video path would be a lie. The UI rejects it with a message.

/** Extensions we allow attaching (lower-case, no dot). Images + text/docs +
 *  common source-code files — all things the model can read by path today. */
const ALLOWED_EXTS = new Set<string>([
  // images (as before)
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif', 'heic', 'heif', 'tiff',
  // docs / structured text
  'txt', 'md', 'markdown', 'pdf', 'csv', 'tsv', 'json', 'jsonl', 'log', 'yaml', 'yml',
  'html', 'htm', 'xml', 'toml', 'ini', 'env', 'conf',
  // source code
  'js', 'jsx', 'mjs', 'cjs', 'ts', 'tsx', 'py', 'go', 'rs', 'java', 'kt', 'kts',
  'c', 'h', 'cpp', 'cc', 'hpp', 'cxx', 'cs', 'rb', 'php', 'swift', 'scala', 'sh',
  'bash', 'zsh', 'sql', 'css', 'scss', 'sass', 'less', 'r', 'lua', 'pl', 'dart',
  'vue', 'svelte', 'proto', 'gradle', 'tf',
]);

/** The extensions above joined for an <input accept> attribute, plus image/*
 *  so mobile pickers still surface the camera/photos shortcuts. */
export const ATTACH_ACCEPT = ['image/*', ...[...ALLOWED_EXTS].map((e) => `.${e}`)].join(',');

/** Pull the lower-cased extension (no dot) from a filename, or '' if none. */
function extOf(name: string): string {
  const dot = name.lastIndexOf('.');
  if (dot <= 0 || dot === name.length - 1) return '';
  return name.slice(dot + 1).toLowerCase();
}

export function isVideoFile(f: File | null | undefined): f is File {
  return !!f && typeof f.type === 'string' && f.type.startsWith('video/');
}

/**
 * Whether a file may be attached to a Hypervisor message. Decided primarily by
 * the filename EXTENSION (that's what Claude Code reads), with a MIME fallback
 * for files that arrive without a useful extension (e.g. clipboard blobs).
 * Video is always rejected — the model can't read it.
 */
export function isAllowedFile(f: File | null | undefined): f is File {
  if (!f) return false;
  const type = (f.type || '').toLowerCase();
  if (type.startsWith('video/')) return false;
  const ext = extOf(f.name || '');
  if (ext) return ALLOWED_EXTS.has(ext);
  // No extension — fall back to MIME for the readable-by-path types.
  if (type.startsWith('image/')) return true;
  if (type.startsWith('text/')) return true;
  return type === 'application/json' || type === 'application/pdf';
}

/**
 * Resolve the on-disk extension for an uploaded file. PREFERS the original
 * filename's extension (a .pdf must stay .pdf or Claude reads it wrong), and
 * only falls back to the MIME map — and finally 'png' — for extension-less
 * blobs, which in practice are always pasted images.
 */
export function extForFile(f: File): string {
  const ext = extOf(f.name || '');
  if (ext) return ext;
  return MIME_EXT[(f.type || '').toLowerCase()] ?? 'png';
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

/** Pull ALL file items (any type) out of a paste's clipboard items. The
 *  caller filters with isAllowedFile so rejected types still get feedback. */
export function filesFromClipboard(data: DataTransfer | null): File[] {
  if (!data) return [];
  const out: File[] = [];
  for (const item of Array.from(data.items)) {
    if (item.kind === 'file') {
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
 * Upload a pasted/dropped/picked file into the task's attachments dir and
 * return the absolute on-disk path Claude Code will read. The saved filename
 * keeps a correct extension (the original's when present, else derived from
 * the MIME type — see MIME_EXT note above) so the read tool classifies it
 * right: a .pdf stays a .pdf, a .txt stays a .txt.
 */
export async function uploadTaskFile(taskId: string, file: File): Promise<string> {
  const ext = extForFile(file);
  const stamp = `${Date.now().toString(36)}-${(_seq++).toString(36)}`;
  const name = `pasted-${stamp}.${ext}`;
  const destPath = `.claude-tasks/${taskId}/attachments`;
  const res = await uploadFile(file, destPath, name);
  return res.absolute_path;
}

/** Back-compat alias — the Build-tab composer still imports uploadTaskImage.
 *  Images resolve their extension the same way, so behaviour is unchanged. */
export const uploadTaskImage = uploadTaskFile;
