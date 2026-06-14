import { describe, it, expect } from 'vitest';
import { extractUrls, shortenUrl } from './terminalUrls';

describe('extractUrls', () => {
  it('returns [] for empty / urlless text', () => {
    expect(extractUrls('')).toEqual([]);
    expect(extractUrls('no links here, just words')).toEqual([]);
  });

  it('pulls a single http(s) url', () => {
    expect(extractUrls('open https://example.com/login to sign in')).toEqual([
      'https://example.com/login',
    ]);
  });

  it('strips trailing punctuation', () => {
    expect(extractUrls('see https://example.com/page.')).toEqual([
      'https://example.com/page',
    ]);
  });

  it('returns freshest-first and dedupes', () => {
    // Space-separated so each is a distinct token (a newline before a
    // URL-safe char would be treated as a soft-wrap rejoin).
    const text = 'first https://a.com/1 later https://b.com/2 again https://a.com/1';
    // Freshest occurrence wins; a.com/1 appears last so it leads, b.com/2 next.
    expect(extractUrls(text)).toEqual(['https://a.com/1', 'https://b.com/2']);
  });

  it('honors the max cap', () => {
    const text = 'https://a.com https://b.com https://c.com';
    expect(extractUrls(text, 2)).toHaveLength(2);
  });

  it('rejoins a soft-wrapped url', () => {
    const text = 'https://example.com/very/long/\n  path/continues';
    expect(extractUrls(text)).toEqual(['https://example.com/very/long/path/continues']);
  });

  it('strips ANSI fragments', () => {
    expect(extractUrls('[32mhttps://example.com[0m')).toEqual([
      'https://example.com',
    ]);
  });
});

describe('shortenUrl', () => {
  it('leaves short urls untouched', () => {
    expect(shortenUrl('https://example.com/x')).toBe('https://example.com/x');
  });

  it('shortens long urls keeping the host + tail', () => {
    const long = 'https://example.com/' + 'a'.repeat(80) + '/end';
    const short = shortenUrl(long);
    expect(short.length).toBeLessThan(long.length);
    expect(short).toContain('example.com');
    expect(short).toContain('/end');
  });
});
