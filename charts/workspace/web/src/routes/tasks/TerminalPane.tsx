import { useEffect, useRef, useState } from 'preact/hooks';
import { prepareTerminal, terminalUrl, vncUrl, openLocalhostPort } from '../../api/tasks';
import { previewFullscreen } from '../../store/ui';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';

export interface TerminalPaneProps {
  taskId: string;
  /** When true, renders ttyd on the left and the noVNC viewer on the right. */
  withVnc?: boolean;
}

const LAST_PORT_KEY = 'kc.previewPort';

/**
 * Wires the workspace's shared ttyd entrypoint to this task's tmux session
 * (via /api/claude/tasks/{id}/prepare-terminal) and then loads the terminal
 * in an iframe. Matches the legacy dashboard's Chat + Preview tabs.
 *
 * In withVnc (Preview) mode, also exposes a port input that POSTs to
 * /api/open-localhost so the in-pod kiosk Chrome navigates to localhost:<port>
 * and the noVNC viewer reflects it. The dashboard never touches port-forwarding.
 */
export function TerminalPane({ taskId, withVnc = false }: TerminalPaneProps) {
  const [phase, setPhase] = useState<'preparing' | 'ready' | 'error'>('preparing');
  const [err, setErr] = useState<string>('');
  const [termSrc, setTermSrc] = useState<string>('');
  const [vncSrc, setVncSrc] = useState<string>('');

  // Preview-only port controls
  const [port, setPort] = useState<string>(() => {
    try {
      return localStorage.getItem(LAST_PORT_KEY) ?? '8080';
    } catch {
      return '8080';
    }
  });
  const [portStatus, setPortStatus] = useState<string>('');
  const [portBusy, setPortBusy] = useState(false);
  const vncRef = useRef<HTMLIFrameElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPhase('preparing');
    setErr('');
    prepareTerminal(taskId)
      .then(() => {
        if (cancelled) return;
        setTermSrc(terminalUrl());
        if (withVnc) setVncSrc(vncUrl());
        setPhase('ready');
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setErr(e instanceof Error ? e.message : 'Failed to attach to tmux session');
        setPhase('error');
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, withVnc]);

  function reattach() {
    setPhase('preparing');
    prepareTerminal(taskId)
      .then(() => {
        setTermSrc(terminalUrl());
        if (withVnc) setVncSrc(vncUrl());
        setPhase('ready');
      })
      .catch((e: unknown) => {
        setErr(e instanceof Error ? e.message : 'Reattach failed');
        setPhase('error');
      });
  }

  /**
   * Bounce the VNC iframe through about:blank then a cache-busted URL so
   * Chromium reliably reloads. Same trick the legacy dashboard uses.
   */
  function refreshVnc() {
    setVncSrc('about:blank');
    requestAnimationFrame(() => setVncSrc(vncUrl()));
  }

  async function openPort(e: Event) {
    e.preventDefault();
    const n = parseInt(port, 10);
    if (!n || n < 1 || n > 65535) {
      setPortStatus('Enter a port 1-65535');
      return;
    }
    setPortBusy(true);
    setPortStatus(`→ localhost:${n}…`);
    try {
      const r = await openLocalhostPort(n);
      if (r && 'error' in r) {
        setPortStatus(`open failed: ${r.error}`);
      } else {
        setPortStatus(`→ localhost:${n}`);
        try { localStorage.setItem(LAST_PORT_KEY, String(n)); } catch { /* noop */ }
        refreshVnc();
      }
    } catch (err) {
      setPortStatus(err instanceof Error ? `error: ${err.message}` : 'error');
    } finally {
      setPortBusy(false);
    }
  }

  return (
    <div class={`term-pane ${withVnc ? 'term-pane-split' : ''}`}>
      <div class="term-pane-bar muted">
        <span class={`term-pane-dot term-pane-dot-${phase}`} aria-hidden="true" />
        <span class="mono">{phase === 'ready' ? 'attached' : phase}</span>

        {withVnc && (
          <form class="term-pane-port" onSubmit={openPort} title="Open localhost:<port> in the in-pod Chrome (right pane)">
            <span class="term-pane-port-label mono">localhost:</span>
            <input
              type="number"
              class="term-pane-port-input mono"
              min={1}
              max={65535}
              value={port}
              onInput={(e) => setPort((e.target as HTMLInputElement).value)}
              aria-label="Localhost port to open in the in-pod browser"
              disabled={portBusy}
            />
            <Button
              type="submit"
              size="sm"
              variant="secondary"
              disabled={portBusy}
              title="Point the in-pod browser at this port"
            >
              Open
            </Button>
            {portStatus && (
              <span class="term-pane-port-status mono" aria-live="polite">
                {portStatus}
              </span>
            )}
          </form>
        )}

        <span class="term-pane-grow" />
        <Button size="sm" variant="ghost" onClick={reattach} disabled={phase === 'preparing'} title="Re-prepare the tmux session and reload the terminal iframe">
          <Icon name="play" size={12} /> Reattach
        </Button>
        {withVnc && (
          <Button size="sm" variant="ghost" onClick={refreshVnc} title="Reload the noVNC viewer">
            Refresh view
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          onClick={() => window.open(terminalUrl(), '_blank', 'noopener')}
          title="Open the terminal in its own browser tab"
        >
          New tab
        </Button>
        {withVnc && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => (previewFullscreen.value = !previewFullscreen.value)}
            aria-label={previewFullscreen.value ? 'Exit fullscreen' : 'Fullscreen'}
            title={previewFullscreen.value ? 'Exit fullscreen (Esc)' : 'Hide rail + master list — give Preview the full viewport'}
          >
            <Icon name={previewFullscreen.value ? 'fullscreen-exit' : 'fullscreen'} size={12} />
            {previewFullscreen.value ? ' Exit' : ' Fullscreen'}
          </Button>
        )}
      </div>

      {phase === 'error' && (
        <div class="term-pane-error">
          <strong>Could not attach:</strong> {err}
          <div class="muted" style={{ marginTop: 4, fontSize: 12 }}>
            The task may have ended, or the workspace is missing the ttyd entrypoint.
          </div>
        </div>
      )}

      {phase !== 'error' && (
        <div class={`term-pane-frames ${withVnc ? 'term-pane-frames-split' : ''}`}>
          <iframe
            class="term-pane-iframe"
            src={termSrc || 'about:blank'}
            title="Task terminal"
            allow="clipboard-read; clipboard-write"
          />
          {withVnc && (
            <iframe
              ref={vncRef}
              class="term-pane-iframe term-pane-iframe-vnc"
              src={vncSrc || 'about:blank'}
              title="Workspace browser (VNC)"
              allow="clipboard-read; clipboard-write"
            />
          )}
        </div>
      )}
    </div>
  );
}
