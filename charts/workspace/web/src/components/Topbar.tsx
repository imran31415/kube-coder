import { theme, paletteOpen, pushToast } from '../store/ui';
import { Button } from './primitives/Button';
import { Icon } from './Icon';
import { HealthDot } from './HealthDot';
import { MetricsBar } from './MetricsBar';
import { MutatorOnly, ReadOnlyPill } from './MutatorOnly';
import { createTerminalTask, terminalUrl } from '../api/tasks';
import { refreshTasks } from '../store/tasks';
import { navigate } from '../store/router';
import { drawerOpen, sheetOpen } from '../store/ui';
import './Topbar.css';

/**
 * Opens a bare ttyd session in a new tab. Registers a plain-bash task first
 * (same flow as the legacy dashboard's openTerminal) so the session shows up
 * in the task list and can be re-attached if its tab is closed.
 *
 * window.open() is called synchronously inside the click handler so popup
 * blockers don't kill it; we navigate the pre-opened tab to the terminal URL
 * once the POST resolves. Cannot use 'noopener' here — it forces the API to
 * return null and we'd lose the handle on the new tab.
 */
async function openNewTerminal() {
  const win = window.open('about:blank', '_blank');
  if (win) {
    // Make sure the opener can't be navigated by the popup once it points
    // to /oauth/terminal/ — the same protection 'noopener' gives, applied
    // after we've kept the handle.
    win.opener = null;
  }
  try {
    await createTerminalTask();
    void refreshTasks();
    pushToast('Terminal session registered.', { kind: 'success' });
  } catch (e) {
    pushToast(e instanceof Error ? e.message : 'Could not register terminal task', { kind: 'warn' });
  }
  const url = terminalUrl();
  if (win && !win.closed) {
    win.location.replace(url);
  } else {
    // Popup blocked OR user closed it during the await — fall back to
    // navigating the current tab so the user still gets to a terminal.
    window.location.href = url;
  }
}

export function Topbar() {
  const isDark = theme.value !== 'light';
  return (
    <header class="topbar" role="banner">
      <button
        type="button"
        class="brand brand-button"
        onClick={() => {
          // Close any open overlays first, then return to the home route so
          // the brand mark consistently behaves as a "back to home" anchor.
          drawerOpen.value = null;
          sheetOpen.value = null;
          paletteOpen.value = false;
          navigate('/tasks');
        }}
        aria-label="Go to home (Build)"
        title="Home — Build sessions"
      >
        <span class="brand-mark" aria-hidden>kc</span>
        <span class="brand-name">kube-coder</span>
        <span class="brand-tag" aria-label="next-generation dashboard">next</span>
        <HealthDot />
      </button>
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
        <ReadOnlyPill />
        <MetricsBar />
        <MutatorOnly>
          <a
            class="topbar-link topbar-link-strong"
            href="/oauth/vscode/?folder=/home/dev"
            target="_blank"
            rel="noopener"
            title="Open VS Code (code-server) at /home/dev"
          >
            VS Code ↗
          </a>
          <button
            type="button"
            class="topbar-link topbar-link-strong"
            onClick={openNewTerminal}
            title="Register a plain-bash task and open ttyd in a new tab"
          >
            New terminal ↗
          </button>
        </MutatorOnly>
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
          onClick={() => (theme.value = isDark ? 'light' : 'dark')}
          title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
        >
          <Icon name={isDark ? 'sun' : 'moon'} size={16} />
        </Button>
      </div>
    </header>
  );
}
