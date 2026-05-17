import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  _resetDocsStore,
  defaultPageId,
  flatPages,
  loadManifest,
  loadPage,
  manifest,
  currentPage,
  pageError,
} from './docs';

const MANIFEST = {
  version: 1,
  sections: [
    { id: 'a', title: 'Section A', pages: [
      { id: 'page-1', title: 'Page 1', file: 'a/p1.md', summary: 's1' },
      { id: 'page-2', title: 'Page 2', file: 'a/p2.md', summary: 's2' },
    ] },
    { id: 'b', title: 'Section B', pages: [
      { id: 'page-3', title: 'Page 3', file: 'b/p3.md', summary: 's3' },
    ] },
  ],
};

const PAGE_1 = {
  id: 'page-1',
  title: 'Page 1',
  summary: 's1',
  section_id: 'a',
  section_title: 'Section A',
  file: 'a/p1.md',
  edited_at: 1700000000,
  markdown: '# hi',
};

describe('store/docs', () => {
  beforeEach(() => {
    _resetDocsStore();
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/api/docs')) {
        return new Response(JSON.stringify(MANIFEST), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (url.endsWith('/api/docs/page-1')) {
        return new Response(JSON.stringify(PAGE_1), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (url.endsWith('/api/docs/missing')) {
        return new Response(JSON.stringify({ error: 'not found' }), { status: 404, headers: { 'Content-Type': 'application/json' } });
      }
      throw new Error(`unmocked: ${url}`);
    });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    _resetDocsStore();
  });

  it('loads the manifest exactly once', async () => {
    await loadManifest();
    await loadManifest();
    expect((globalThis.fetch as ReturnType<typeof vi.fn>)).toHaveBeenCalledTimes(1);
    expect(manifest.value?.sections.length).toBe(2);
  });

  it('computes defaultPageId and flatPages from the manifest', async () => {
    await loadManifest();
    expect(defaultPageId.value).toBe('page-1');
    expect(flatPages.value.map((p) => p.id)).toEqual(['page-1', 'page-2', 'page-3']);
    expect(flatPages.value[0].section).toBe('Section A');
  });

  it('loadPage caches subsequent reads of the same id', async () => {
    await loadManifest();
    await loadPage('page-1');
    await loadPage('page-1');
    // 1 manifest + 1 page fetch only.
    expect((globalThis.fetch as ReturnType<typeof vi.fn>)).toHaveBeenCalledTimes(2);
    expect(currentPage.value?.id).toBe('page-1');
  });

  it('records pageError on 404', async () => {
    await loadManifest();
    await loadPage('missing');
    expect(currentPage.value).toBeNull();
    expect(pageError.value).toMatch(/not found/i);
  });
});
