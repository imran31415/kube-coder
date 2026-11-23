import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { _resetDocsStore } from '../../store/docs';
import { currentPath, navigate } from '../../store/router';
import { renderMarkdown, resolveDocAsset, slugify } from './Article';
import { DocsRoute } from './index';
import { withOauthPrefix } from '../../api/client';

const asset = (p: string) => withOauthPrefix(`/api/docs/assets/${p}`);
// jsdom's DOMPurify unwraps the very first element of a fragment (a real
// browser keeps it), so markdown under test starts with a throwaway line.
const LEAD = 'lead\n\n';

const MANIFEST = {
  version: 1,
  sections: [
    { id: 'a', title: 'Section A', pages: [
      { id: 'page-1', title: 'Page 1', file: 'in-app/p1.md', summary: 'first summary' },
      { id: 'page-2', title: 'Page 2', file: 'p2.md', summary: 'second summary' },
    ] },
    { id: 'b', title: 'Section B', pages: [
      { id: 'page-3', title: 'Page 3', file: 'in-app/p3.md', summary: 'third summary' },
    ] },
  ],
};

function page(id: string, title: string, section: string, markdown: string) {
  return { id, title, summary: '', section_id: 'x', section_title: section, file: 'p.md', edited_at: 1, markdown };
}
const PAGES: Record<string, unknown> = {
  'page-1': page('page-1', 'Page 1', 'Section A', '## Body of one'),
  'page-2': page('page-2', 'Page 2', 'Section A', '## Body of two'),
  'page-3': page('page-3', 'Page 3', 'Section B', '## Body of three'),
};

describe('resolveDocAsset', () => {
  it('resolves a relative image against the page file directory', () => {
    expect(resolveDocAsset('screenshots/board/a.png', 'boards.md'))
      .toBe(asset('screenshots/board/a.png'));
    expect(resolveDocAsset('./img/x.png', 'in-app/files.md'))
      .toBe(asset('in-app/img/x.png'));
    expect(resolveDocAsset('../screenshots/x.png', 'in-app/files.md'))
      .toBe(asset('screenshots/x.png'));
  });

  it('percent-encodes path segments', () => {
    expect(resolveDocAsset('shots/my shot.png', 'a.md')).toBe(asset('shots/my%20shot.png'));
  });

  it('leaves absolute, root, data and escaping URLs alone', () => {
    for (const src of ['https://x.test/a.png', '/favicon.svg', 'data:image/png;base64,AA', '#frag']) {
      expect(resolveDocAsset(src, 'in-app/a.md')).toBe(src);
    }
    expect(resolveDocAsset('../../etc/x.png', 'in-app/a.md')).toBe('../../etc/x.png');
  });
});

describe('slugify', () => {
  it('matches GitHub heading anchors the docs already link to', () => {
    expect(slugify('Connecting a board')).toBe('connecting-a-board');
    expect(slugify('Working on a copy (schema / migration work)'))
      .toBe('working-on-a-copy-schema--migration-work');
    expect(slugify('Undelivered prompts (prompt reaper)')).toBe('undelivered-prompts-prompt-reaper');
  });
});

describe('renderMarkdown', () => {
  it('gives headings unique GitHub-style ids, including ones DOMPurify would clobber', () => {
    const html = renderMarkdown(LEAD + '## Title\n\n## Setup\n\n### Setup\n\n## `code` & more');
    expect(html).toContain('<h2 id="title">Title</h2>');
    expect(html).toContain('<h2 id="setup">Setup</h2>');
    expect(html).toContain('<h3 id="setup-1">Setup</h3>');
    expect(html).toContain('id="code--more"');
  });

  it('rewrites relative images to the asset route and lazy-loads them', () => {
    const html = renderMarkdown(LEAD + '![shot](screenshots/a.png)\n\n![ext](https://x.test/b.png)', 'boards.md');
    expect(html).toContain(`src="${asset('screenshots/a.png')}"`);
    expect(html).toMatch(/<img[^>]*loading="lazy"/);
    expect(html).toContain('src="https://x.test/b.png"');
  });

  it('wraps tables in a scroll container', () => {
    const html = renderMarkdown(LEAD + '| a | b |\n|---|---|\n| 1 | 2 |');
    expect(html).toMatch(/<div class="docs-table-wrap"><table>/);
  });
});

describe('DocsRoute', () => {
  const originalMatchMedia = window.matchMedia;

  function viewport(mobile: boolean) {
    window.matchMedia = ((query: string) => ({
      matches: mobile && query.includes('max-width: 720px'),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  }

  beforeEach(() => {
    _resetDocsStore();
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      const id = url.match(/\/api\/docs\/([^/?]+)$/)?.[1];
      const body = id ? PAGES[id] : MANIFEST;
      return new Response(JSON.stringify(body ?? { error: 'nope' }), {
        status: body ? 200 : 404,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
    vi.restoreAllMocks();
    _resetDocsStore();
    navigate('/docs', true);
  });

  it('phone: bare /docs is a table of contents, not the first article', async () => {
    viewport(true);
    navigate('/docs', true);
    render(<DocsRoute />);
    await screen.findByRole('link', { name: /Page 3/ });
    expect(screen.getByText('Section A')).toBeInTheDocument();
    expect(screen.getByText('second summary')).toBeInTheDocument();
    expect(document.querySelector('.docs-article')).toBeNull();
    // Only the manifest is fetched — the index doesn't preload page one.
    const urls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => /\/api\/docs\/page-/.test(u))).toBe(false);

    fireEvent.click(screen.getByRole('link', { name: /Page 2/ }));
    expect(currentPath.value).toBe('/docs/page-2');
  });

  it('phone: an article has a back bar and previous/next links', async () => {
    viewport(true);
    navigate('/docs/page-2', true);
    render(<DocsRoute />);
    await screen.findByRole('heading', { level: 1, name: 'Page 2' });
    expect(screen.queryByRole('navigation', { name: 'Documentation sections' })).toBeNull();

    const pager = screen.getByRole('navigation', { name: 'More docs' });
    expect(pager).toHaveTextContent('Page 1');
    expect(pager).toHaveTextContent('Page 3');

    fireEvent.click(screen.getByRole('link', { name: /Next/ }));
    expect(currentPath.value).toBe('/docs/page-3');

    fireEvent.click(screen.getByRole('link', { name: /All docs/ }));
    expect(currentPath.value).toBe('/docs');
  });

  it('desktop: bare /docs keeps the sidebar and opens the first page, no pager', async () => {
    viewport(false);
    navigate('/docs', true);
    render(<DocsRoute />);
    await screen.findByRole('heading', { level: 1, name: 'Page 1' });
    expect(screen.getByRole('navigation', { name: 'Documentation sections' })).toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'More docs' })).toBeNull();
    expect(screen.queryByRole('link', { name: /All docs/ })).toBeNull();
  });

  it('scrolls to an in-page anchor instead of changing the route', async () => {
    viewport(true);
    PAGES['page-1'] = page('page-1', 'Page 1', 'Section A', 'See [below](#the-end).\n\n## The end');
    const scrolled: string[] = [];
    const orig = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = function (this: HTMLElement) { scrolled.push(this.id); };
    try {
      navigate('/docs/page-1', true);
      render(<DocsRoute />);
      const link = await screen.findByRole('link', { name: 'below' });
      fireEvent.click(link);
      await waitFor(() => expect(scrolled).toEqual(['the-end']));
      expect(currentPath.value).toBe('/docs/page-1');
    } finally {
      HTMLElement.prototype.scrollIntoView = orig;
    }
  });
});
