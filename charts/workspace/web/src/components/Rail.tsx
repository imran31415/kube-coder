import { useEffect } from 'preact/hooks';
import {
  currentPath,
  navigate,
  matchRoute,
  NAV_GROUPS,
  navGroupFor,
  navLabel,
  type NavGroup,
} from '../store/router';
import {
  railCollapsed,
  previewFullscreen,
  collapsedRailGroups,
  toggleRailGroup,
  expandRailGroup,
} from '../store/ui';
import { Icon, type IconName } from './Icon';
import './Rail.css';

const ICONS: Record<string, IconName> = {
  '/tasks': 'tasks',
  '/desktop': 'desktop',
  '/mission': 'mission',
  '/cto': 'cto',
  '/hypervisor': 'hypervisor',
  '/walkie': 'walkie',
  '/memory': 'memory',
  '/skills': 'skills',
  '/apps': 'apps',
  '/triggers': 'triggers',
  '/files': 'files',
  '/docs': 'docs',
  '/settings': 'settings',
};

function RailItem({ path, active }: { path: string; active: string }) {
  const isActive = active === path;
  const label = navLabel(path);
  return (
    <button
      type="button"
      class={`rail-item ${isActive ? 'rail-item-active' : ''}`}
      onClick={() => navigate(path)}
      aria-current={isActive ? 'page' : undefined}
      title={railCollapsed.value ? label : undefined}
    >
      <Icon name={ICONS[path] ?? 'inbox'} size={16} />
      <span class="rail-item-label">{label}</span>
    </button>
  );
}

function RailGroup({ group, active }: { group: NavGroup; active: string }) {
  const expanded = !collapsedRailGroups.value.includes(group.id);
  const containsActive =
    group.landing === active || group.items.some((i) => i.path === active);
  // When a collapsed group holds the active route, tint the header so the
  // user never loses orientation.
  const headerTint = !expanded && containsActive ? 'rail-group-header-active' : '';
  const chevron = (
    <Icon name={expanded ? 'chevron-down' : 'chevron-right'} size={12} />
  );
  return (
    <div class="rail-group">
      {group.landing ? (
        // Landing groups (Mission Control): the label navigates, the
        // chevron toggles — two separate buttons for two separate actions.
        // The link renders as a regular rail item (icon + label) so it
        // reads as clickable, not as a section heading.
        <div class={`rail-group-header ${headerTint}`}>
          <button
            type="button"
            class={`rail-item rail-group-link ${active === group.landing ? 'rail-item-active' : ''}`}
            onClick={() => navigate(group.landing!)}
            aria-current={active === group.landing ? 'page' : undefined}
          >
            <Icon name={ICONS[group.landing] ?? 'inbox'} size={16} />
            <span class="rail-item-label">{group.title}</span>
          </button>
          <button
            type="button"
            class="rail-group-chev"
            onClick={() => toggleRailGroup(group.id)}
            aria-expanded={expanded}
            aria-label={`${expanded ? 'Collapse' : 'Expand'} ${group.title}`}
          >
            {chevron}
          </button>
        </div>
      ) : (
        <div class={`rail-group-header ${headerTint}`}>
          <button
            type="button"
            class="rail-group-label"
            onClick={() => toggleRailGroup(group.id)}
            aria-expanded={expanded}
          >
            <span class="rail-group-title">{group.title}</span>
            {chevron}
          </button>
        </div>
      )}
      {expanded && (
        <div class="rail-group-items" role="group" aria-label={group.title}>
          {group.items.map((i) => (
            <RailItem key={i.path} path={i.path} active={active} />
          ))}
        </div>
      )}
    </div>
  );
}

export function Rail() {
  const active = matchRoute(currentPath.value).path;
  const collapsed = railCollapsed.value;

  // Auto-expand the group containing the active route (never auto-collapse
  // others) so palette/bottom-nav jumps always land on a visible item.
  useEffect(() => {
    const g = navGroupFor(active);
    if (g) expandRailGroup(g.id);
  }, [active]);

  // Preview fullscreen hides the rail entirely (overrides collapse state).
  if (previewFullscreen.value) return null;
  return (
    <nav
      class={`rail ${collapsed ? 'rail-collapsed' : ''}`}
      aria-label="Primary"
      data-collapsed={collapsed ? 'true' : 'false'}
    >
      <div class="rail-items">
        {collapsed
          ? // Icon-only mode: groups flatten to icon runs separated by
            // hairline dividers; no disclosure. The Mission landing icon
            // leads its group.
            NAV_GROUPS.map((g, idx) => (
              <div class="rail-group" key={g.id}>
                {idx > 0 && <div class="rail-divider" role="separator" />}
                {g.landing && <RailItem path={g.landing} active={active} />}
                {g.items.map((i) => (
                  <RailItem key={i.path} path={i.path} active={active} />
                ))}
              </div>
            ))
          : NAV_GROUPS.map((g) => <RailGroup key={g.id} group={g} active={active} />)}
        {/* Settings + Collapse flow with the nav as a final group (#448):
            a bottom slab pinned via margin-top:auto gets pushed below the
            fold once the rail overflows and starts scrolling. */}
        <div class="rail-group">
          <div class="rail-divider" role="separator" />
          <RailItem path="/settings" active={active} />
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
        </div>
      </div>
    </nav>
  );
}
