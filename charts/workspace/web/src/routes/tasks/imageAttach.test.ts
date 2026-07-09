import { describe, it, expect } from 'vitest';
import { extForImageMime, isImageFile, imagesFromClipboard } from './imageAttach';

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
