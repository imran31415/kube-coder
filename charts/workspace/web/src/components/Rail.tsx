import { currentPath, navigate, ROUTES, matchRoute } from '../store/router';
import { railCollapsed, previewFullscreen, drawerOpen } from '../store/ui';
import { Icon, type IconName } from './Icon';
import { MutatorOnly } from './MutatorOnly';
import './Rail.css';

const ICONS: Record<string, IconName> = {
  '/tasks': 'tasks',
  '/desktop': 'desktop',
  '/memory': 'memory',
  '/apps': 'apps',
  '/triggers': 'triggers',
  '/files': 'files',
  '/docs': 'docs',
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
      {/* Primary CTA at the top of the rail. Sits above the route list with
          a 1px separator so it reads as a distinct action, not a nav item.
          Earlier "+" icon-only button next to the Build row was too easy
          to miss; this is the discoverable replacement. Hidden in
          read-only public-demo via MutatorOnly. */}
      <MutatorOnly>
        <div class="rail-cta">
          <button
            type="button"
            class="rail-cta-btn"
            onClick={() => {
              if (active !== '/tasks') navigate('/tasks');
              drawerOpen.value = 'new-task';
            }}
            title="New build"
            aria-label="New build"
          >
            <Icon name="plus" size={14} />
            {!collapsed && <span class="rail-cta-label">New build</span>}
          </button>
        </div>
        <div class="rail-sep" aria-hidden="true" />
      </MutatorOnly>
      <div class="rail-items">
        {ROUTES.map((r) => {
          const isActive = active === r.path;
          return (
            <button
              key={r.path}
              type="button"
              class={`rail-item ${isActive ? 'rail-item-active' : ''}`}
              onClick={() => navigate(r.path)}
              aria-current={isActive ? 'page' : undefined}
              title={collapsed ? r.title : undefined}
            >
              <Icon name={ICONS[r.path] ?? 'inbox'} size={16} />
              <span class="rail-item-label">{r.title}</span>
            </button>
          );
        })}
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
