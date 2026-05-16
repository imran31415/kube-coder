import { useState } from 'preact/hooks';
import { launchInPodBrowser, openLocalhostPort, vncUrl } from '../../api/tasks';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { pushToast } from '../../store/ui';

/**
 * Standalone browser/VNC controls in Settings — for when the user wants to
 * fire up the in-pod browser without selecting a task. The per-task split
 * view with embedded VNC lives in the Tasks Preview tab.
 */
export function BrowserSection() {
  const [port, setPort] = useState('8080');
  const [busy, setBusy] = useState(false);

  async function onLaunch() {
    setBusy(true);
    try {
      const r = await launchInPodBrowser();
      if (r && 'error' in r) pushToast(`Launch failed: ${r.error}`, { kind: 'danger' });
      else pushToast('Browser launched in pod.', { kind: 'success' });
    } catch (e) {
      pushToast(e instanceof Error ? e.message : 'Launch failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  async function onOpenPort(e: Event) {
    e.preventDefault();
    const n = parseInt(port, 10);
    if (!n || n < 1 || n > 65535) {
      pushToast('Enter a port 1-65535', { kind: 'warn' });
      return;
    }
    setBusy(true);
    try {
      const r = await openLocalhostPort(n);
      if (r && 'error' in r) pushToast(`Open failed: ${r.error}`, { kind: 'danger' });
      else pushToast(`Pointed in-pod browser at localhost:${n}.`, { kind: 'success' });
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Open failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  function openVncWindow() {
    // Standalone full-window VNC viewer — different from the Tasks Preview
    // tab which embeds it in a side-by-side split.
    window.open(vncUrl(), '_blank', 'noopener');
  }

  return (
    <section class="settings-section">
      <h2 class="settings-section-title">Browser & desktop</h2>
      <p class="settings-row-hint muted">
        Controls for the workspace pod's in-X-display browser. The Tasks Preview
        tab embeds the same VNC view alongside a per-task terminal.
      </p>

      <div class="settings-row">
        <div class="settings-row-label">In-pod browser</div>
        <div class="settings-row-control">
          <Button
            onClick={onLaunch}
            disabled={busy}
            title="Spawn Chrome/Chromium (or fallback browser) on the pod's X display"
          >
            <Icon name="play" size={14} /> Launch browser
          </Button>
        </div>
      </div>

      <div class="settings-row">
        <div class="settings-row-label">Open port</div>
        <div class="settings-row-control">
          <form onSubmit={onOpenPort} class="settings-portform">
            <span class="muted mono">localhost:</span>
            <input
              type="number"
              class="settings-portform-input mono"
              min={1}
              max={65535}
              value={port}
              onInput={(e) => setPort((e.target as HTMLInputElement).value)}
              aria-label="Localhost port to open in the in-pod browser"
              disabled={busy}
            />
            <Button
              type="submit"
              disabled={busy}
              title="Tell the in-pod browser to navigate to this URL"
            >
              Open
            </Button>
          </form>
          <p class="settings-row-hint muted" style={{ marginTop: 4 }}>
            Useful for previewing a local dev server running inside the workspace.
          </p>
        </div>
      </div>

      <div class="settings-row">
        <div class="settings-row-label">Remote desktop</div>
        <div class="settings-row-control">
          <Button
            variant="secondary"
            onClick={openVncWindow}
            title="Open the noVNC viewer in a new tab — full window, not embedded"
          >
            Open VNC in new tab
          </Button>
        </div>
      </div>
    </section>
  );
}
