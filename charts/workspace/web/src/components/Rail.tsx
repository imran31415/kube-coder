import { currentPath, navigate, ROUTES, matchRoute } from '../store/router';
import { railCollapsed, previewFullscreen, drawerOpen } from '../store/ui';
import { Icon, type IconName } from './Icon';
import { MutatorOnly } from './MutatorOnly';
import './Rail.css';

const ICONS: Record<string, IconName> = {
  '/tasks': 'tasks',
  '/memory': 'memory',
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
      <div class="rail-items">
        {ROUTES.map((r) => {
          const isActive = active === r.path;
          // Build row gets an inline "+" that opens the new-build drawer —
          // replaces the old route-header "New build" button, reclaiming the
          // vertical space at the top of the Tasks route. Hidden when the
          // rail is collapsed to a 52px icon strip.
          if (r.path === '/tasks' && !collapsed) {
            return (
              <div key={r.path} class="rail-row">
                <button
                  type="button"
                  class={`rail-item ${isActive ? 'rail-item-active' : ''}`}
                  onClick={() => navigate(r.path)}
                  aria-current={isActive ? 'page' : undefined}
                >
                  <Icon name={ICONS[r.path] ?? 'inbox'} size={16} />
                  <span class="rail-item-label">{r.title}</span>
                </button>
                <MutatorOnly>
                  <button
                    type="button"
                    class="rail-item-action"
                    onClick={() => {
                      if (!isActive) navigate('/tasks');
                      drawerOpen.value = 'new-task';
                    }}
                    aria-label="New build"
                    title="New build"
                  >
                    <Icon name="plus" size={14} />
                  </button>
                </MutatorOnly>
              </div>
            );
          }
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
