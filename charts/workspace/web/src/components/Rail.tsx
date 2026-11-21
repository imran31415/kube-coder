import { useEffect } from 'preact/hooks';
import {
  currentPath,
  navigate,
  matchRoute,
  visibleNavGroups,
  inWorkspace,
  navLabel,
  NAV_TOP,
  WORKSPACE_GROUP_ID,
  WORKSPACE_TAIL,
  type NavGroup,
} from '../store/router';
import { serverMode } from '../store/server-mode';
import {
  railCollapsed,
  previewFullscreen,
  isRailGroupExpanded,
  toggleRailGroup,
  expandRailGroup,
} from '../store/ui';
import { Icon, type IconName } from './Icon';
import './Rail.css';

const ICONS: Record<string, IconName> = {
  '/tasks': 'tasks',
  '/desktop': 'desktop',
  '/mission': 'mission',
  '/board': 'board',
  '/feed': 'feed',
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

function RailItem({
  path,
  active,
  label: labelOverride,
}: {
  path: string;
  active: string;
  /** Rail-only label (e.g. a section's landing reads "Overview"). */
  label?: string;
}) {
  const isActive = active === path;
  const label = labelOverride ?? navLabel(path);
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

/** One sub-heading inside Workspace: a plain heading over its items. Not a
 *  disclosure of its own — one level of collapse is the point of this rail. */
function RailSection({ group, active }: { group: NavGroup; active: string }) {
  if (!group.landing && group.items.length === 0) return null;
  return (
    <div class="rail-section" role="group" aria-label={group.title}>
      <div class="rail-section-title" aria-hidden="true">
        {group.title}
      </div>
      {group.landing && <RailItem path={group.landing} active={active} label="Overview" />}
      {group.items.map((i) => (
        <RailItem key={i.path} path={i.path} active={active} />
      ))}
    </div>
  );
}

/** Icon-only counterpart of RailSection: an icon run under a hairline. */
function RailIconRun({ group, active }: { group: NavGroup; active: string }) {
  return (
    <>
      <div class="rail-divider" role="separator" />
      {group.landing && <RailItem path={group.landing} active={active} label={group.title} />}
      {group.items.map((i) => (
        <RailItem key={i.path} path={i.path} active={active} />
      ))}
    </>
  );
}

/**
 * The Workspace disclosure: every destination that is not Chat. Collapsed
 * by default; it reveals itself while the active route lives inside it
 * (Rail's effect → expandRailGroup) and, if the user explicitly collapses it
 * while standing there, the header takes the accent tint instead so they
 * never lose their place.
 */
function WorkspaceGroup({
  groups,
  active,
  collapsed,
}: {
  groups: NavGroup[];
  active: string;
  collapsed: boolean;
}) {
  const id = WORKSPACE_GROUP_ID;
  const expanded = isRailGroupExpanded(id);
  const containsActive = inWorkspace(active);
  const headerTint = !expanded && containsActive ? 'rail-group-header-active' : '';
  const title = NAV_TOP[1].title;
  return (
    <div class="rail-group rail-workspace">
      <div class={`rail-group-header ${headerTint}`}>
        <button
          type="button"
          class="rail-item rail-group-toggle"
          onClick={() => toggleRailGroup(id)}
          aria-expanded={expanded}
          aria-controls="rail-workspace-items"
          title={collapsed ? `${expanded ? 'Collapse' : 'Expand'} ${title}` : undefined}
        >
          <Icon name="workspace" size={16} />
          <span class="rail-item-label">{title}</span>
          {!collapsed && (
            <span class="rail-group-chev" aria-hidden="true">
              <Icon name={expanded ? 'chevron-down' : 'chevron-right'} size={12} />
            </span>
          )}
        </button>
      </div>
      {expanded && (
        <div
          id="rail-workspace-items"
          class="rail-group-items rail-workspace-items"
          role="group"
          aria-label={title}
        >
          {collapsed
            ? groups.map((g) => <RailIconRun key={g.id} group={g} active={active} />)
            : groups.map((g) => <RailSection key={g.id} group={g} active={active} />)}
          <div class="rail-divider" role="separator" />
          {WORKSPACE_TAIL.map((p) => (
            <RailItem key={p} path={p} active={active} />
          ))}
        </div>
      )}
    </div>
  );
}

export function Rail() {
  const active = matchRoute(currentPath.value).path;
  const collapsed = railCollapsed.value;
  // Hide capability-gated items (AI CTO when disabled server-side, #467).
  const groups = visibleNavGroups({
    ctoEnabled: serverMode.value.ctoEnabled,
    boardEnabled: serverMode.value.boardEnabled,
    scansEnabled: serverMode.value.scansEnabled,
  });

  // Reveal Workspace while the active route lives inside it, so palette /
  // bottom-nav jumps always land on a visible item. Transient by design (see
  // store/ui.ts): going back to Chat re-collapses it, so the two-row default
  // survives normal use instead of creeping back to sixteen.
  useEffect(() => {
    expandRailGroup(inWorkspace(active) ? WORKSPACE_GROUP_ID : null);
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
        {/* Exactly two top-level entries: Chat, then Workspace (NAV_TOP). */}
        <div class="rail-group">
          <RailItem path={NAV_TOP[0].path} active={active} label={NAV_TOP[0].title} />
        </div>
        <WorkspaceGroup groups={groups} active={active} collapsed={collapsed} />
        {/* Collapse flows with the nav as a final group (#448): a bottom slab
            pinned via margin-top:auto gets pushed below the fold once the
            rail overflows and starts scrolling. */}
        <div class="rail-group">
          <div class="rail-divider" role="separator" />
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
