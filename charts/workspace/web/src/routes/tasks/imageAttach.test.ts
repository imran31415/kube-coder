import { describe, it, expect, afterEach, vi } from 'vitest';
import {
  extForImageMime,
  extForFile,
  isImageFile,
  isAllowedFile,
  isVideoFile,
  imagesFromClipboard,
  filesFromClipboard,
  readClipboard,
  ATTACH_ACCEPT,
} from './imageAttach';

const file = (name: string, type = '') => new File([''], name, { type });

describe('extForImageMime', () => {
  it('maps common image MIME types to their extension', () => {
    expect(extForImageMime('image/png')).toBe('png');
    expect(extForImageMime('image/jpeg')).toBe('jpg');
    expect(extForImageMime('image/webp')).toBe('webp');
    expect(extForImageMime('image/gif')).toBe('gif');
    expect(extForImageMime('image/svg+xml')).toBe('svg');
  });

  it('is case-insensitive', () => {
    expect(extForImageMime('IMAGE/PNG')).toBe('png');
  });

  it('falls back to png for unknown / empty types', () => {
    expect(extForImageMime('')).toBe('png');
    expect(extForImageMime('application/octet-stream')).toBe('png');
  });
});

describe('isImageFile', () => {
  it('accepts image/* files and rejects everything else', () => {
    expect(isImageFile(new File([''], 'a.png', { type: 'image/png' }))).toBe(true);
    expect(isImageFile(new File([''], 'a.txt', { type: 'text/plain' }))).toBe(false);
    expect(isImageFile(null)).toBe(false);
    expect(isImageFile(undefined)).toBe(false);
  });
});

describe('isVideoFile', () => {
  it('accepts video/* and rejects the rest', () => {
    expect(isVideoFile(file('clip.mp4', 'video/mp4'))).toBe(true);
    expect(isVideoFile(file('a.png', 'image/png'))).toBe(false);
    expect(isVideoFile(null)).toBe(false);
  });
});

describe('isAllowedFile', () => {
  it('accepts docs / text / structured files by extension', () => {
    for (const n of ['notes.txt', 'README.md', 'doc.markdown', 'report.pdf', 'data.csv', 'x.json', 'app.log', 'k8s.yaml', 'c.yml', 'page.html', 'feed.xml']) {
      expect(isAllowedFile(file(n))).toBe(true);
    }
  });

  it('accepts common source-code files', () => {
    for (const n of ['a.ts', 'b.tsx', 'c.js', 'main.py', 'srv.go', 'lib.rs', 'App.java', 'q.sql', 'style.css']) {
      expect(isAllowedFile(file(n))).toBe(true);
    }
  });

  it('accepts images (extension or MIME)', () => {
    expect(isAllowedFile(file('a.png', 'image/png'))).toBe(true);
    expect(isAllowedFile(file('pasted', 'image/jpeg'))).toBe(true); // no extension, MIME wins
  });

  it('rejects video with any extension or MIME', () => {
    expect(isAllowedFile(file('movie.mp4', 'video/mp4'))).toBe(false);
    expect(isAllowedFile(file('movie.mov'))).toBe(false);
    expect(isAllowedFile(file('novideoext', 'video/webm'))).toBe(false);
  });

  it('rejects unsupported binary types', () => {
    expect(isAllowedFile(file('archive.zip', 'application/zip'))).toBe(false);
    expect(isAllowedFile(file('a.exe', 'application/octet-stream'))).toBe(false);
    expect(isAllowedFile(file('noext'))).toBe(false);
    expect(isAllowedFile(null)).toBe(false);
    expect(isAllowedFile(undefined)).toBe(false);
  });
});

describe('extForFile', () => {
  it('prefers the original filename extension (kept lower-case)', () => {
    expect(extForFile(file('report.pdf', 'application/pdf'))).toBe('pdf');
    expect(extForFile(file('NOTES.MD', 'text/markdown'))).toBe('md');
    expect(extForFile(file('a.txt', 'text/plain'))).toBe('txt');
    expect(extForFile(file('Component.tsx'))).toBe('tsx');
  });

  it('keeps the real extension even when the MIME would map elsewhere', () => {
    // A .pdf must never be rewritten to .png just because MIME is unknown.
    expect(extForFile(file('doc.pdf', 'application/octet-stream'))).toBe('pdf');
  });

  it('falls back to the MIME map for extension-less blobs', () => {
    expect(extForFile(file('pasted', 'image/png'))).toBe('png');
    expect(extForFile(file('pasted', 'image/jpeg'))).toBe('jpg');
  });

  it('falls back to png only for extension-less unknown blobs', () => {
    expect(extForFile(file('pasted', ''))).toBe('png');
  });
});

describe('ATTACH_ACCEPT', () => {
  it('includes image/* and representative document/code extensions', () => {
    expect(ATTACH_ACCEPT).toContain('image/*');
    expect(ATTACH_ACCEPT).toContain('.pdf');
    expect(ATTACH_ACCEPT).toContain('.md');
    expect(ATTACH_ACCEPT).toContain('.tsx');
  });
});

describe('imagesFromClipboard', () => {
  // Minimal DataTransferItem stub — jsdom's clipboard items aren't populated
  // for synthetic paste events, so we model just the shape the helper reads.
  function item(kind: string, type: string, file: File | null): DataTransferItem {
    return { kind, type, getAsFile: () => file } as unknown as DataTransferItem;
  }
  function data(items: DataTransferItem[]): DataTransfer {
    return { items } as unknown as DataTransfer;
  }

  it('returns [] for null clipboard data', () => {
    expect(imagesFromClipboard(null)).toEqual([]);
  });

  it('extracts only image file items', () => {
    const png = new File([''], 'x.png', { type: 'image/png' });
    const out = imagesFromClipboard(
      data([
        item('string', 'text/plain', null),
        item('file', 'image/png', png),
        item('file', 'application/pdf', new File([''], 'y.pdf', { type: 'application/pdf' })),
      ]),
    );
    expect(out).toEqual([png]);
  });

  it('skips image items whose getAsFile returns null', () => {
    expect(imagesFromClipboard(data([item('file', 'image/png', null)]))).toEqual([]);
  });

  it('filesFromClipboard returns every file item regardless of type', () => {
    const png = new File([''], 'x.png', { type: 'image/png' });
    const pdf = new File([''], 'y.pdf', { type: 'application/pdf' });
    const out = filesFromClipboard(
      data([
        item('string', 'text/plain', null), // not a file → skipped
        item('file', 'image/png', png),
        item('file', 'application/pdf', pdf),
        item('file', 'application/zip', null), // null file → skipped
      ]),
    );
    expect(out).toEqual([png, pdf]);
  });

  it('filesFromClipboard returns [] for null data', () => {
    expect(filesFromClipboard(null)).toEqual([]);
  });
});

describe('readClipboard', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // Stub navigator.clipboard with just the surface readClipboard() touches.
  function stubClipboard(impl: Partial<Clipboard> & { read?: () => Promise<ClipboardItem[]> }) {
    vi.stubGlobal('navigator', { clipboard: impl });
  }
  function clipItem(types: string[], blobs: Record<string, Blob>): ClipboardItem {
    return { types, getType: (t: string) => Promise.resolve(blobs[t]) } as unknown as ClipboardItem;
  }

  it('reads an image blob as a File with a MIME-correct extension', async () => {
    const blob = new Blob(['x'], { type: 'image/png' });
    stubClipboard({ read: () => Promise.resolve([clipItem(['image/png'], { 'image/png': blob })]) });
    const { text, images } = await readClipboard();
    expect(text).toBe('');
    expect(images).toHaveLength(1);
    expect(images[0].type).toBe('image/png');
    expect(images[0].name).toBe('pasted.png');
  });

  it('reads text/plain items into text', async () => {
    const blob = new Blob(['hello there'], { type: 'text/plain' });
    stubClipboard({ read: () => Promise.resolve([clipItem(['text/plain'], { 'text/plain': blob })]) });
    const { text, images } = await readClipboard();
    expect(text).toBe('hello there');
    expect(images).toEqual([]);
  });

  it('falls back to readText() when read() is unavailable', async () => {
    stubClipboard({ readText: () => Promise.resolve('plain fallback') });
    const { text, images } = await readClipboard();
    expect(text).toBe('plain fallback');
    expect(images).toEqual([]);
  });

  it('falls back to readText() when read() throws', async () => {
    stubClipboard({
      read: () => Promise.reject(new Error('blocked')),
      readText: () => Promise.resolve('after throw'),
    });
    const { text, images } = await readClipboard();
    expect(text).toBe('after throw');
    expect(images).toEqual([]);
  });

  it('returns empty when the clipboard is fully unavailable', async () => {
    stubClipboard({});
    expect(await readClipboard()).toEqual({ text: '', images: [] });
  });
});
