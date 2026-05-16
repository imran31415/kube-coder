import { currentPath, navigate, ROUTES, matchRoute } from '../store/router';
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
  return (
    <nav class="rail" aria-label="Primary">
      {ROUTES.map((r) => (
        <button
          key={r.path}
          class={`rail-item ${active === r.path ? 'rail-item-active' : ''}`}
          onClick={() => navigate(r.path)}
          aria-current={active === r.path ? 'page' : undefined}
        >
          <Icon name={ICONS[r.path] ?? 'inbox'} size={16} />
          <span>{r.title}</span>
        </button>
      ))}
    </nav>
  );
}
