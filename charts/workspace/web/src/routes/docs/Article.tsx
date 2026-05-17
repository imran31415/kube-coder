import { useEffect, useMemo, useRef } from 'preact/hooks';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { navigate } from '../../store/router';
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
  const html = useMemo(() => renderMarkdown(page.markdown), [page.markdown]);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // Reset scroll to top when switching pages.
    const main = document.querySelector('.app-main');
    if (main) main.scrollTop = 0;
  }, [page.id]);

  function onClick(e: MouseEvent) {
    const target = (e.target as HTMLElement | null)?.closest?.('a') as HTMLAnchorElement | null;
    if (!target) return;
    const href = target.getAttribute('href') || '';
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

function renderMarkdown(src: string): string {
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
  return DOMPurify.sanitize(html, {
    ADD_ATTR: ['target', 'rel'],
    USE_PROFILES: { html: true },
  });
}
