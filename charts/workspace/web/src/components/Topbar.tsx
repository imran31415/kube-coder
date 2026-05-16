import { theme, paletteOpen } from '../store/ui';
import { Button } from './primitives/Button';
import { Icon } from './Icon';
import { HealthDot } from './HealthDot';
import './Topbar.css';

export function Topbar() {
  const isDark = theme.value !== 'light';
  return (
    <header class="topbar" role="banner">
      <div class="brand">
        <span class="brand-mark" aria-hidden>kc</span>
        <span class="brand-name">kube-coder</span>
        <span class="brand-tag" aria-label="next-generation dashboard">next</span>
        <HealthDot />
      </div>
      <button
        type="button"
        class="topbar-search"
        onClick={() => (paletteOpen.value = true)}
        aria-label="Open command palette"
      >
        <Icon name="search" size={14} />
        <span>Search or jump to…</span>
        <kbd class="topbar-kbd">⌘K</kbd>
      </button>
      <div class="topbar-actions">
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
          onClick={() => (theme.value = isDark ? 'light' : 'dark')}
        >
          <Icon name={isDark ? 'sun' : 'moon'} size={16} />
        </Button>
        <a class="topbar-link" href="/dashboard-legacy" aria-label="Open legacy dashboard">
          Legacy ↗
        </a>
      </div>
    </header>
  );
}
