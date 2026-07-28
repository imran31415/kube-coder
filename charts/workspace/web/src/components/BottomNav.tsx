import { currentPath, navigate, matchRoute, navLabel } from '../store/router';
import { sheetOpen } from '../store/ui';
import { Icon, type IconName } from './Icon';
import './BottomNav.css';

interface Slot {
  path: string;
  icon: IconName;
}

// Labels come from navLabel() so the bottom bar, the rail and the command
// palette can't drift apart (#346 — this bar said "Chat" while the router
// still called the same route "Hypervisor").
const SLOTS: Slot[] = [
  // Desktop is the default landing on the SPA — give it the first slot
  // here too so primary nav is consistent across the rail + bottom bar.
  { path: '/desktop', icon: 'desktop' },
  { path: '/hypervisor', icon: 'hypervisor' },
  { path: '/tasks', icon: 'tasks' },
  { path: '/memory', icon: 'memory' },
];

// "More" sheet absorbs anything not in SLOTS — apps, triggers, files,
// docs, settings. Highlights when the current route is one of those.
const MORE_ROUTES = new Set(['/mission', '/cto', '/feed', '/walkie', '/skills', '/apps', '/triggers', '/files', '/docs', '/settings']);

export function BottomNav() {
  const active = matchRoute(currentPath.value).path;
  return (
    <nav class="bottomnav" aria-label="Primary mobile">
      {SLOTS.map((s) => (
        <button
          key={s.path}
          class={`bn-item ${active === s.path ? 'bn-item-active' : ''}`}
          onClick={() => navigate(s.path)}
          aria-current={active === s.path ? 'page' : undefined}
        >
          <Icon name={s.icon} size={20} />
          <span class="bn-label">{navLabel(s.path)}</span>
        </button>
      ))}
      <button
        class={`bn-item ${MORE_ROUTES.has(active) ? 'bn-item-active' : ''}`}
        onClick={() => (sheetOpen.value = 'more')}
        aria-label="More"
      >
        <Icon name="more" size={20} />
        <span class="bn-label">More</span>
      </button>
    </nav>
  );
}
