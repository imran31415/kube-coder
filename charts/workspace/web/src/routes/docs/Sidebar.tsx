import { navigate, routeHref } from '../../store/router';
import { manifest } from '../../store/docs';
import type { DocsManifest, DocsPageMeta } from '../../api/docs';
import { Icon } from '../../components/Icon';

interface SidebarProps {
  currentId: string | null;
  onPick?: () => void;
}

export function DocsSidebar({ currentId, onPick }: SidebarProps) {
  const m = manifest.value;
  if (!m) return null;
  return (
    <nav class="docs-sidebar" aria-label="Documentation sections">
      {m.sections.map((sec) => (
        <div key={sec.id} class="docs-section">
          <div class="docs-section-title">{sec.title}</div>
          <ul class="docs-page-list">
            {sec.pages.map((p) => {
              const active = p.id === currentId;
              return (
                <li key={p.id}>
                  <button
                    type="button"
                    class={`docs-page-link ${active ? 'docs-page-link-active' : ''}`}
                    aria-current={active ? 'page' : undefined}
                    onClick={() => {
                      navigate(`/docs/${p.id}`);
                      onPick?.();
                    }}
                  >
                    <span class="docs-page-link-title">{p.title}</span>
                    {p.summary && <span class="docs-page-link-summary muted">{p.summary}</span>}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

/**
 * Phone landing for `/docs`: every page as a full-width, thumb-sized row,
 * grouped by section. Replaces the desktop sidebar on small screens so the
 * table of contents is a page of its own instead of a box squeezed above the
 * article (index → article → back, like a native settings list).
 */
export function DocsIndex() {
  const m = manifest.value;
  if (!m) return null;
  return (
    <nav class="docs-index" aria-label="Documentation">
      {m.sections.map((sec) => (
        <section key={sec.id} class="docs-index-section" aria-labelledby={`docs-sec-${sec.id}`}>
          <h2 class="docs-index-section-title" id={`docs-sec-${sec.id}`}>{sec.title}</h2>
          <ul class="docs-index-list">
            {sec.pages.map((p) => (
              <li key={p.id}>
                <a
                  class="docs-index-row"
                  href={routeHref(`/docs/${p.id}`)}
                  onClick={(e) => {
                    e.preventDefault();
                    navigate(`/docs/${p.id}`);
                  }}
                >
                  <span class="docs-index-row-text">
                    <span class="docs-index-row-title">{p.title}</span>
                    {p.summary && <span class="docs-index-row-summary">{p.summary}</span>}
                  </span>
                  <span class="docs-index-row-chevron" aria-hidden="true">
                    <Icon name="chevron-right" size={18} />
                  </span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </nav>
  );
}

/** Reading order across sections, as the manifest lists it. */
export function flatPages(m: DocsManifest | null): DocsPageMeta[] {
  return m ? m.sections.flatMap((s) => s.pages) : [];
}

/** Previous / next page links shown under the article on phones. */
export function DocsPager({ currentId }: { currentId: string }) {
  const pages = flatPages(manifest.value);
  const i = pages.findIndex((p) => p.id === currentId);
  if (i < 0) return null;
  const prev = pages[i - 1];
  const next = pages[i + 1];
  if (!prev && !next) return null;
  const go = (id: string) => (e: MouseEvent) => {
    e.preventDefault();
    navigate(`/docs/${id}`);
  };
  return (
    <nav class="docs-pager" aria-label="More docs">
      {prev ? (
        <a class="docs-pager-link docs-pager-prev" href={routeHref(`/docs/${prev.id}`)} onClick={go(prev.id)}>
          <span class="docs-pager-dir">
            <Icon name="chevron-left" size={14} /> Previous
          </span>
          <span class="docs-pager-title">{prev.title}</span>
        </a>
      ) : <span />}
      {next ? (
        <a class="docs-pager-link docs-pager-next" href={routeHref(`/docs/${next.id}`)} onClick={go(next.id)}>
          <span class="docs-pager-dir">
            Next <Icon name="chevron-right" size={14} />
          </span>
          <span class="docs-pager-title">{next.title}</span>
        </a>
      ) : <span />}
    </nav>
  );
}
