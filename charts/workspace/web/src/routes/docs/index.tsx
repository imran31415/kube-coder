import { useEffect } from 'preact/hooks';
import { currentPath, pathSuffix } from '../../store/router';
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
import { DocsSidebar } from './Sidebar';
import { DocsArticle } from './Article';
import './docs.css';

export function DocsRoute() {
  const isMobile = useIsMobile();

  useEffect(() => {
    void loadManifest();
  }, []);

  // Whenever the URL or default changes, load the matching page.
  useEffect(() => {
    const suffix = pathSuffix(currentPath.value);
    const targetId = suffix || defaultPageId.value;
    if (targetId) void loadPage(targetId);
  }, [currentPath.value, defaultPageId.value]);

  const m = manifest.value;
  const cur = currentPage.value;

  return (
    <div class="route route-docs">
      <header class="route-header">
        <h1 class="route-title">Docs</h1>
        <p class="route-subtitle muted">
          Learn how every part of your workspace works. Source markdown lives under
          <code> /home/dev/kube-coder/docs/</code> — edit it there and refresh.
        </p>
      </header>

      {manifestLoading.value && !m && <div class="docs-loading muted">Loading documentation…</div>}
      {manifestError.value && (
        <div class="docs-error" role="alert">
          Couldn't load documentation: {manifestError.value}
          <button type="button" class="docs-error-retry" onClick={() => void loadManifest(true)}>Retry</button>
        </div>
      )}

      {m && (
        <div class={`docs-layout ${isMobile ? 'docs-layout-mobile' : ''}`}>
          {!isMobile && (
            <aside class="docs-sidebar-wrap">
              <DocsSidebar currentId={cur?.id ?? null} />
            </aside>
          )}
          <section class="docs-article-wrap">
            {isMobile && (
              <details class="docs-mobile-toc">
                <summary>Browse all pages</summary>
                <DocsSidebar currentId={cur?.id ?? null} />
              </details>
            )}
            {pageLoading.value && !cur && <div class="docs-loading muted">Loading page…</div>}
            {pageError.value && (
              <div class="docs-error" role="alert">
                {pageError.value}
              </div>
            )}
            {cur && <DocsArticle page={cur} />}
          </section>
        </div>
      )}
    </div>
  );
}
