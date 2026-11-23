import { useEffect, useMemo, useRef } from 'preact/hooks';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { navigate } from '../../store/router';
import { withOauthPrefix } from '../../api/client';
import type { DocsPage } from '../../api/docs';

interface ArticleProps {
  page: DocsPage;
}

/**
 * Renders a docs page's markdown. Custom blockquote handling promotes
 * `> :::scenario` … `> :::` blocks to a styled callout. Internal links
 * to `/docs/...` are intercepted so they navigate within the SPA.
 */
export function DocsArticle({ page }: ArticleProps) {
  // The header already renders `page.title` as the article <h1>. Many docs
  // bodies open with their own duplicate `# Title`, which would render a second
  // identical heading — strip a leading H1 that matches the page title.
  const html = useMemo(
    () => renderMarkdown(stripDuplicateTitle(page.markdown, page.title), page.file),
    [page.markdown, page.title, page.file],
  );
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // Reset scroll to top when switching pages. The window is what actually
    // scrolls (the shell grows with its content); .app-main is kept for any
    // layout where it is height-constrained.
    const main = document.querySelector('.app-main');
    if (main) main.scrollTop = 0;
    window.scrollTo(0, 0);
  }, [page.id]);

  function onClick(e: MouseEvent) {
    const target = (e.target as HTMLElement | null)?.closest?.('a') as HTMLAnchorElement | null;
    if (!target) return;
    const href = target.getAttribute('href') || '';
    // In-page anchors: scroll the heading into view inside the app scroller
    // (scroll-margin-top keeps it clear of the phone's sticky back bar).
    if (href.startsWith('#') && href.length > 1) {
      let id = href.slice(1);
      try { id = decodeURIComponent(id); } catch { /* keep raw */ }
      const el = ref.current?.querySelector(`[id="${CSS.escape(id)}"]`);
      if (el) {
        e.preventDefault();
        el.scrollIntoView({ block: 'start' });
      }
      return;
    }
    // Internal docs/SPA links: intercept and route in-app.
    if (href.startsWith('/docs') || href.startsWith('/tasks') || href.startsWith('/memory') ||
        href.startsWith('/triggers') || href.startsWith('/files') || href.startsWith('/settings')) {
      e.preventDefault();
      navigate(href);
      return;
    }
    // External links: open in new tab so the SPA isn't lost.
    if (/^https?:\/\//.test(href)) {
      target.setAttribute('target', '_blank');
      target.setAttribute('rel', 'noopener noreferrer');
    }
  }

  return (
    <article class="docs-article">
      <header class="docs-article-header">
        <div class="docs-article-breadcrumbs muted">
          {page.section_title} <span class="docs-crumb-sep">/</span> {page.title}
        </div>
        <h1 class="docs-article-title">{page.title}</h1>
        {page.summary && <p class="docs-article-summary muted">{page.summary}</p>}
      </header>
      <div
        class="docs-article-body"
        ref={ref}
        onClick={onClick}
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </article>
  );
}

/**
 * Drop a leading `# Heading` from the markdown body when it duplicates the
 * page title (which the header already renders as an <h1>). Only a top-of-body
 * H1 is considered — leading blank lines are skipped, deeper headings and any
 * H1 further down are left untouched. Matching is case- and whitespace-lenient.
 */
function stripDuplicateTitle(src: string, title: string): string {
  const lines = src.split('\n');
  let i = 0;
  while (i < lines.length && lines[i].trim() === '') i++;
  const m = lines[i]?.match(/^#\s+(.*)$/);
  if (m && m[1].trim().toLowerCase() === title.trim().toLowerCase()) {
    lines.splice(0, i + 1);
    return lines.join('\n');
  }
  return src;
}

/**
 * Promote `> :::scenario` callout blocks before handing off to marked.
 * The body inside the callout is rendered as a separate markdown pass so
 * **bold**, code spans, lists, etc. work inside. Marked's default doesn't
 * re-parse markdown inside raw block-level HTML, hence the two passes.
 */
function preprocess(src: string): { md: string; callouts: string[] } {
  const lines = src.split('\n');
  const out: string[] = [];
  const callouts: string[] = [];
  let buf: string[] = [];
  let inCallout = false;
  for (const raw of lines) {
    const stripped = raw.replace(/^>\s?/, '');
    if (raw.trimStart().startsWith('>') && stripped.trim() === ':::scenario') {
      inCallout = true;
      buf = [];
      continue;
    }
    if (inCallout && raw.trimStart().startsWith('>') && stripped.trim() === ':::') {
      inCallout = false;
      const id = callouts.length;
      callouts.push(buf.join('\n'));
      out.push(`<!--KC_CALLOUT_${id}-->`);
      continue;
    }
    if (inCallout && raw.trimStart().startsWith('>')) {
      buf.push(stripped);
      continue;
    }
    out.push(raw);
  }
  return { md: out.join('\n'), callouts };
}

/**
 * Map an image `src` from a docs page to something the browser can load.
 * Markdown embeds screenshots relative to its own file (boards.md →
 * `screenshots/board-processor/x.png`), but the SPA serves the page at
 * `/docs/<id>`, so the browser would resolve them to `/docs/screenshots/…`
 * and get the app shell. Relative paths are resolved against the page's
 * `file` and served from `/api/docs/assets/`; absolute URLs, root paths,
 * data: URIs and anything climbing out of the docs tree are left as-is.
 */
export function resolveDocAsset(src: string, file: string): string {
  if (!src || /^[a-z][a-z0-9+.-]*:/i.test(src) || src.startsWith('/') || src.startsWith('#')) return src;
  const parts = file.split('/').slice(0, -1);
  for (const seg of src.split(/[?#]/, 1)[0].split('/')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') {
      if (!parts.length) return src;
      parts.pop();
    } else {
      parts.push(seg);
    }
  }
  return withOauthPrefix(`/api/docs/assets/${parts.map(encodeURIComponent).join('/')}`);
}

/**
 * GitHub's heading-anchor algorithm (github-slugger): lowercase, drop
 * punctuation, each space becomes a hyphen. Matching it means the docs'
 * existing `[below](#connecting-a-board)` links work in-app as they do on
 * GitHub. Repeats get `-1`, `-2`… suffixes, also as GitHub does.
 */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .trim()
    .replace(/[^\p{L}\p{M}\p{N}\p{Pc} -]/gu, '')
    .replace(/ /g, '-');
}

function decodeEntities(s: string): string {
  return s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}

function addHeadingIds(html: string): string {
  const seen = new Map<string, number>();
  return html.replace(/<h([1-4])>([\s\S]*?)<\/h\1>/g, (_m, level: string, inner: string) => {
    const base = slugify(decodeEntities(inner.replace(/<[^>]*>/g, '')));
    if (!base) return `<h${level}>${inner}</h${level}>`;
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    const id = n ? `${base}-${n}` : base;
    return `<h${level} id="${id}">${inner}</h${level}>`;
  });
}

export function renderMarkdown(src: string, file = ''): string {
  marked.setOptions({ gfm: true, breaks: false });
  const { md, callouts } = preprocess(src);
  let html = marked.parse(md, { async: false }) as string;
  for (let i = 0; i < callouts.length; i++) {
    const inner = marked.parse(callouts[i], { async: false }) as string;
    const block =
      '<div class="docs-callout docs-callout-scenario">' +
      '<div class="docs-callout-label">Scenario</div>' +
      inner +
      '</div>';
    html = html.replace(`<!--KC_CALLOUT_${i}-->`, block);
  }
  // Wrap every table in a horizontally scrollable container so wide tables
  // can scroll independently of the page layout instead of forcing the
  // article wider than the mobile viewport.
  html = html.replace(/<table>/g, '<div class="docs-table-wrap"><table>')
             .replace(/<\/table>/g, '</table></div>');
  // Point relative screenshots at the docs asset route and let the phone
  // defer the ones far below the fold.
  html = html.replace(/<img src="([^"]*)"/g, (_m, s: string) =>
    `<img loading="lazy" src="${resolveDocAsset(s.replace(/&amp;/g, '&'), file).replace(/&/g, '&amp;')}"`);
  const clean = DOMPurify.sanitize(html, {
    ADD_ATTR: ['target', 'rel'],
    USE_PROFILES: { html: true },
  });
  // Ids go on after sanitizing: DOMPurify's anti-clobbering strips ids such
  // as `title` or `images` that shadow document properties, and a slug is
  // only letters, digits, `_` and `-`, so it cannot smuggle markup back in.
  return labelTableCells(addHeadingIds(clean));
}

/**
 * Tables with 3+ columns (mostly `Field | Type | Required | Description`
 * references) stack into one card per row on phones — squeezed into 360px
 * as a grid, the last column became a 6em strip off-screen. Each cell is
 * tagged with its column header so the card can label it (`td::before`);
 * the wrap gets `docs-table-stack`, which only the phone CSS acts on, so
 * desktop keeps the plain table. Runs on already-sanitized HTML inside an
 * inert <template>, and only sets attributes from header *text*.
 */
function labelTableCells(html: string): string {
  if (!html.includes('<table') || typeof document === 'undefined') return html;
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  tpl.content.querySelectorAll('.docs-table-wrap').forEach((wrap) => {
    const heads = Array.from(wrap.querySelectorAll('thead th')).map((th) => (th.textContent || '').trim());
    if (heads.length < 3) return;
    wrap.classList.add('docs-table-stack');
    wrap.querySelectorAll('tbody tr').forEach((tr) => {
      Array.from(tr.children).forEach((td, i) => {
        if (heads[i]) td.setAttribute('data-label', heads[i]);
      });
    });
  });
  return tpl.innerHTML;
}
