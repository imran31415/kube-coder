import { describe, it, expect, afterEach, vi } from 'vitest';
import { extForImageMime, isImageFile, imagesFromClipboard, readClipboard } from './imageAttach';

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
