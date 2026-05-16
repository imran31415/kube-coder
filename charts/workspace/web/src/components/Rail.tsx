import { currentPath, navigate, ROUTES, matchRoute } from '../store/router';
import { railCollapsed, previewFullscreen } from '../store/ui';
import { Icon, type IconName } from './Icon';
import './Rail.css';

const ICONS: Record<string, IconName> = {
  '/tasks': 'tasks',
  '/memory': 'memory',
  '/triggers': 'triggers',
  '/files': 'files',
  '/settings': 'settings',
};

export function Rail() {
  const active = matchRoute(currentPath.value).path;
  const collapsed = railCollapsed.value;
  // Preview fullscreen hides the rail entirely (overrides collapse state).
  if (previewFullscreen.value) return null;
  return (
    <nav
      class={`rail ${collapsed ? 'rail-collapsed' : ''}`}
      aria-label="Primary"
      data-collapsed={collapsed ? 'true' : 'false'}
    >
      <div class="rail-items">
        {ROUTES.map((r) => (
          <button
            key={r.path}
            type="button"
            class={`rail-item ${active === r.path ? 'rail-item-active' : ''}`}
            onClick={() => navigate(r.path)}
            aria-current={active === r.path ? 'page' : undefined}
            title={collapsed ? r.title : undefined}
          >
            <Icon name={ICONS[r.path] ?? 'inbox'} size={16} />
            <span class="rail-item-label">{r.title}</span>
          </button>
        ))}
      </div>
      <button
        type="button"
        class="rail-toggle"
        onClick={() => (railCollapsed.value = !collapsed)}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      >
        <Icon name={collapsed ? 'chevron-right' : 'chevron-left'} size={14} />
        {!collapsed && <span class="rail-toggle-label">Collapse</span>}
      </button>
    </nav>
  );
}
