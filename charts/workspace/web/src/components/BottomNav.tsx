import { currentPath, navigate, matchRoute, navLabel, inWorkspace, NAV_TOP } from '../store/router';
import { sheetOpen } from '../store/ui';
import { Icon } from './Icon';
import './BottomNav.css';

/**
 * Narrow-screen primary nav — the same two top-level entries as the rail
 * (NAV_TOP): Chat, and Workspace, which opens the Workspace sheet (the
 * rail's disclosure as a bottom sheet: every other destination, grouped the
 * same way). Workspace lights up whenever the active route lives inside it,
 * mirroring the rail header's tint, so a phone user always sees where they are.
 */
export function BottomNav() {
  const active = matchRoute(currentPath.value).path;
  const chat = NAV_TOP[0].path;
  const workspaceActive = inWorkspace(active);
  return (
    <nav class="bottomnav" aria-label="Primary mobile">
      <button
        class={`bn-item ${active === chat ? 'bn-item-active' : ''}`}
        onClick={() => navigate(chat)}
        aria-current={active === chat ? 'page' : undefined}
      >
        <Icon name="hypervisor" size={20} />
        {/* navLabel() keeps the bar, the rail and the palette on one name (#346). */}
        <span class="bn-label">{navLabel(chat)}</span>
      </button>
      <button
        class={`bn-item ${workspaceActive ? 'bn-item-active' : ''}`}
        onClick={() => (sheetOpen.value = 'more')}
        aria-label={NAV_TOP[1].title}
        aria-haspopup="dialog"
      >
        <Icon name="workspace" size={20} />
        <span class="bn-label">{NAV_TOP[1].title}</span>
      </button>
    </nav>
  );
}
