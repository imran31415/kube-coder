import { useEffect } from 'preact/hooks';
import { currentPath, navigate, pathSuffix, routeHref } from '../../store/router';
import {
  currentPage,
  defaultPageId,
  loadManifest,
  loadPage,
  manifest,
  manifestError,
  manifestLoading,
  pageError,
  pageLoading,
} from '../../store/docs';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { Icon } from '../../components/Icon';
import { DocsIndex, DocsPager, DocsSidebar } from './Sidebar';
import { DocsArticle } from './Article';
import './docs.css';

export function DocsRoute() {
  const isMobile = useIsMobile();
  const suffix = pathSuffix(currentPath.value);
  // Phones get a two-level layout: bare /docs is the table of contents, a
  // page id is the article with a back bar. Desktop keeps sidebar + article
  // and opens the first page when no id is given.
  const showIndex = isMobile && !suffix;

  useEffect(() => {
    void loadManifest();
  }, []);

  // Whenever the URL or default changes, load the matching page.
  useEffect(() => {
    if (showIndex) return;
    const targetId = suffix || defaultPageId.value;
    if (targetId) void loadPage(targetId);
  }, [suffix, defaultPageId.value, showIndex]);

  // Returning to the phone index should start at the top, like the article.
  useEffect(() => {
    if (!showIndex) return;
    const main = document.querySelector('.app-main');
    if (main) main.scrollTop = 0;
  }, [showIndex]);

  const m = manifest.value;
  const cur = currentPage.value;
  const showMobileArticle = isMobile && !showIndex;

  return (
    <div class={`route route-docs ${showMobileArticle ? 'route-docs-mobile-article' : ''}`}>
      {showMobileArticle ? (
        <div class="docs-mobile-bar">
          <a
            class="docs-mobile-back"
            href={routeHref('/docs')}
            onClick={(e) => {
              e.preventDefault();
              navigate('/docs');
            }}
          >
            <Icon name="chevron-left" size={20} />
            <span>All docs</span>
          </a>
          {cur && <span class="docs-mobile-bar-section">{cur.section_title}</span>}
        </div>
      ) : (
        <header class="route-header">
          <h1 class="route-title">Docs</h1>
          <p class="route-subtitle muted">
            {isMobile ? (
              'How every part of your workspace works.'
            ) : (
              <>
                Learn how every part of your workspace works. Source markdown lives under
                <code> /home/dev/kube-coder/docs/</code> — edit it there and refresh.
              </>
            )}
          </p>
        </header>
      )}

      {manifestLoading.value && !m && <div class="docs-loading muted">Loading documentation…</div>}
      {manifestError.value && (
        <div class="docs-error" role="alert">
          Couldn't load documentation: {manifestError.value}
          <button type="button" class="docs-error-retry" onClick={() => void loadManifest(true)}>Retry</button>
        </div>
      )}

      {m && showIndex && <DocsIndex />}

      {m && !showIndex && (
        <div class={`docs-layout ${isMobile ? 'docs-layout-mobile' : ''}`}>
          {!isMobile && (
            <aside class="docs-sidebar-wrap">
              <DocsSidebar currentId={cur?.id ?? null} />
            </aside>
          )}
          <section class="docs-article-wrap">
            {pageLoading.value && !cur && <div class="docs-loading muted">Loading page…</div>}
            {pageError.value && (
              <div class="docs-error" role="alert">
                {pageError.value}
              </div>
            )}
            {cur && <DocsArticle page={cur} />}
            {cur && isMobile && <DocsPager currentId={cur.id} />}
          </section>
        </div>
      )}
    </div>
  );
}
