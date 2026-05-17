import { navigate } from '../../store/router';
import { manifest } from '../../store/docs';

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
