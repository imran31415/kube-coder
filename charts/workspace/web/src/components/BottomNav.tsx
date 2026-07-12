import { currentPath, navigate, matchRoute } from '../store/router';
import { sheetOpen } from '../store/ui';
import { Icon, type IconName } from './Icon';
import './BottomNav.css';

interface Slot {
  path: string;
  title: string;
  icon: IconName;
}

const SLOTS: Slot[] = [
  // Desktop is the default landing on the SPA — give it the first slot
  // here too so primary nav is consistent across the rail + bottom bar.
  { path: '/desktop', title: 'Desktop', icon: 'desktop' },
  { path: '/hypervisor', title: 'Chat', icon: 'hypervisor' },
  { path: '/tasks', title: 'Build', icon: 'tasks' },
  { path: '/memory', title: 'Memory', icon: 'memory' },
];

// "More" sheet absorbs anything not in SLOTS — apps, triggers, files,
// docs, settings. Highlights when the current route is one of those.
const MORE_ROUTES = new Set(['/apps', '/triggers', '/files', '/docs', '/settings']);

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
          <span class="bn-label">{s.title}</span>
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
