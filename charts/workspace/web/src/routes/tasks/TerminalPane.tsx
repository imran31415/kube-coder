import { useEffect, useRef, useState } from 'preact/hooks';
import { prepareTerminal, terminalUrl, vncUrl, openLocalhostPort, getTaskOutput } from '../../api/tasks';
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
 * Pull HTTP(S) URLs out of the tmux pane text. Mobile users can't easily
 * highlight + copy from inside the ttyd iframe, and assistants like Claude
 * Code routinely print "open https://… to sign in" prompts — we promote
 * those URLs to tappable buttons above the iframe.
 *
 * Server-side calls `tmux capture-pane -J` so logical lines come back joined
 * already, but we still defensively handle soft-wraps + \r line endings +
 * stray ANSI fragments since the pre-`-J` deploys may still be in flight
 * and the harness/output.log fallback path doesn't have that protection.
 *
 * Walk-forward parser: anchor at `https?://`, then greedily consume URL-safe
 * chars, allowing newlines (optionally surrounded by whitespace) to bridge
 * soft-wraps. Stops at the first hard delimiter or end of buffer.
 */
const URL_CHAR_RE = /[A-Za-z0-9._~:/?#@!$&'*+,;=%-]/;
const URL_ANCHOR_RE = /https?:\/\//g;
const TRAILING_PUNCT = /[.,;:!?'"`)\]}>]+$/;
const ANSI_RE = /\[[0-9;?]*[a-zA-Z]/g;

function extractUrls(text: string, max = 5): string[] {
  if (!text) return [];
  // Normalize line endings + strip any ANSI escapes that leaked through.
  const norm = text.replace(/\r\n?/g, '\n').replace(ANSI_RE, '');
  const seen = new Set<string>();
  const out: string[] = [];

  let m: RegExpExecArray | null;
  URL_ANCHOR_RE.lastIndex = 0;
  const found: string[] = [];
  while ((m = URL_ANCHOR_RE.exec(norm)) !== null) {
    let i = m.index + m[0].length;
    let url = m[0];
    while (i < norm.length) {
      const ch = norm[i];
      if (ch === '\n') {
        // Soft-wrap: peek past any whitespace on the next line. If the next
        // non-space char is URL-safe, treat the wrap as join-back.
        let j = i + 1;
        while (j < norm.length && (norm[j] === ' ' || norm[j] === '\t')) j++;
        if (j < norm.length && URL_CHAR_RE.test(norm[j])) {
          i = j;
          continue;
        }
        break;
      }
      // Plain whitespace inside a line is a hard URL terminator.
      if (ch === ' ' || ch === '\t') break;
      if (URL_CHAR_RE.test(ch)) {
        url += ch;
        i++;
        continue;
      }
      break;
    }
    // Strip trailing punctuation that's almost never part of a URL.
    url = url.replace(TRAILING_PUNCT, '');
    if (url.length > m[0].length + 3) found.push(url);
  }

  // Freshest URL first.
  for (let i = found.length - 1; i >= 0 && out.length < max; i--) {
    const u = found[i];
    if (seen.has(u)) continue;
    seen.add(u);
    out.push(u);
  }
  return out;
}

function shortenUrl(u: string, max = 56): string {
  if (u.length <= max) return u;
  try {
    const url = new URL(u);
    const host = url.host;
    const tail = u.slice(host.length + url.protocol.length + 2);
    const cut = Math.max(8, max - host.length - 5);
    return `${host}/…${tail.slice(-cut)}`;
  } catch {
    return u.slice(0, max - 1) + '…';
  }
}

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

  // Tappable URL strip — refreshed every ~4s from the task's tmux pane.
  // Skipped in Preview (withVnc) mode since the VNC viewer is the primary
  // surface there and the strip would feel redundant.
  const [urls, setUrls] = useState<string[]>([]);
  const [copiedUrl, setCopiedUrl] = useState<string | null>(null);
  useEffect(() => {
    if (withVnc) return;
    let cancelled = false;
    async function pull() {
      try {
        const r = await getTaskOutput(taskId, 60);
        if (cancelled) return;
        setUrls(extractUrls(r.output ?? ''));
      } catch {
        /* silent — keep last-good URLs */
      }
    }
    void pull();
    const id = window.setInterval(pull, 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [taskId, withVnc]);

  async function copyUrl(u: string) {
    try {
      await navigator.clipboard.writeText(u);
      setCopiedUrl(u);
      setTimeout(() => setCopiedUrl((cur) => (cur === u ? null : cur)), 1500);
    } catch { /* clipboard unavailable */ }
  }

  useEffect(() => {
    let cancelled = false;
    // Force iframe through about:blank so the previous task's ttyd
    // connection drops before we open a new one. Without this we'd
    // sometimes land on the previous task's tmux pane (or a bare bash
    // shell) until the user manually clicked the tab again.
    setTermSrc('about:blank');
    if (withVnc) setVncSrc('about:blank');
    setPhase('preparing');
    setErr('');
    prepareTerminal(taskId)
      .then(() => {
        if (cancelled) return;
        // One animation frame after the POST resolves so the entry-file
        // write has hit disk before ttyd reconnects.
        requestAnimationFrame(() => {
          if (cancelled) return;
          setTermSrc(terminalUrl());
          if (withVnc) setVncSrc(vncUrl());
          setPhase('ready');
        });
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

      {!withVnc && urls.length > 0 && (
        <div class="term-pane-urls" aria-label="URLs detected in terminal output">
          <span class="term-pane-urls-label muted">Links from terminal</span>
          <ul class="term-pane-urls-list">
            {urls.map((u) => (
              <li key={u} class="term-pane-urls-item">
                <a
                  class="term-pane-urls-link"
                  href={u}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={u}
                >
                  <Icon name="chevron-right" size={11} />
                  <span class="mono">{shortenUrl(u)}</span>
                </a>
                <button
                  type="button"
                  class="term-pane-urls-copy"
                  onClick={() => void copyUrl(u)}
                  title="Copy URL to clipboard"
                  aria-label={`Copy ${u}`}
                >
                  {copiedUrl === u ? 'copied' : 'copy'}
                </button>
              </li>
            ))}
          </ul>
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
