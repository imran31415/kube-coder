import { useEffect, useRef, useState } from 'preact/hooks';
import { prepareTerminal, terminalUrl, vncUrl, openLocalhostPort, getTaskOutput, setScrollMode } from '../../api/tasks';
import { uploadFile } from '../../api/files';
import { sendFollowup } from '../../store/tasks';
import { previewFullscreen, pushToast } from '../../store/ui';
import { MutatorOnly } from '../../components/MutatorOnly';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';

export interface TerminalPaneProps {
  taskId: string;
  /** When true, renders ttyd on the left and the noVNC viewer on the right. */
  withVnc?: boolean;
}

const LAST_PORT_KEY = 'kc.previewPort';
const LAST_PATH_KEY = 'kc.previewPath';

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

  // Upload "+" button — lets the user drop a file into the running task's
  // workspace directly from the Session/Preview top bar. Mobile users
  // couldn't easily get a local file into the Claude session before; they
  // had to switch to the Files route, upload, then come back and message
  // the path. Now: tap +, pick file, file lands at
  // /home/dev/uploads/<task_id>/<name> and a brief notice gets pasted
  // into the tmux pane so Claude knows it's there.
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  async function onUploadFile(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    setUploading(true);
    const destDir = `uploads/${taskId}`;
    try {
      await uploadFile(file, destDir);
      const fullPath = `/home/dev/${destDir}/${file.name}`;
      pushToast(`Uploaded ${file.name} → ${fullPath}`, { kind: 'success' });
      // Best-effort: tell the active session about it so the user doesn't
      // have to copy/paste the path. Failure here is non-fatal (the file
      // is on disk regardless), so just log a softer toast.
      try {
        await sendFollowup(
          taskId,
          `I uploaded a file to your workspace: ${fullPath}`,
        );
      } catch {
        pushToast('Upload OK, but could not notify the session — paste the path manually.', { kind: 'warn' });
      }
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Upload failed', { kind: 'danger' });
    } finally {
      setUploading(false);
      input.value = '';
    }
  }

  // Scroll mode toggle — flips the tmux pane in/out of copy-mode via a
  // server-side `tmux copy-mode` / `send-keys -X cancel`. Replaces the
  // user holding Ctrl+B [ to scroll back and `q` to exit. Once in
  // copy-mode, arrow keys / Page Up / wheel drive the scrollback (even
  // when the inner app is on alt-screen — copy-mode's bindings win).
  const [scrollMode, setScrollModeState] = useState<boolean>(false);
  const [scrollBusy, setScrollBusy] = useState(false);
  async function toggleScrollMode() {
    const next: 'enter' | 'exit' = scrollMode ? 'exit' : 'enter';
    setScrollBusy(true);
    try {
      await setScrollMode(taskId, next);
      setScrollModeState(next === 'enter');
      if (next === 'enter') {
        pushToast('Scroll mode on — arrows / PgUp / wheel to scroll, click button again to exit.', { kind: 'info', ttl: 5000 });
      }
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Could not toggle scroll mode', { kind: 'danger' });
    } finally {
      setScrollBusy(false);
    }
  }
  // Reset scroll-mode state when the task changes — the server tracks
  // tmux state per session so we don't actually need to "exit" the old
  // task's mode before switching, but the button label should reset.
  useEffect(() => { setScrollModeState(false); }, [taskId]);

  // Preview-only port + path controls. Path defaults to "/" so the
  // existing port-only flow keeps working, but the user can now drop in
  // /admin, /?dev=1, etc. and the in-pod browser will navigate there
  // instead of just the host root. Both persisted to localStorage so
  // the next Preview open inherits the last setting per workspace.
  const [port, setPort] = useState<string>(() => {
    try { return localStorage.getItem(LAST_PORT_KEY) ?? '8080'; }
    catch { return '8080'; }
  });
  const [urlPath, setUrlPath] = useState<string>(() => {
    try { return localStorage.getItem(LAST_PATH_KEY) ?? '/'; }
    catch { return '/'; }
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
        // Cap at 1 — the strip sits above the terminal iframe and a long
        // list eats too much of the visible terminal area. Freshest URL
        // wins (extractUrls iterates bottom-up), which is what the user
        // needs for time-sensitive flows like Claude's oauth/authorize.
        setUrls(extractUrls(r.output ?? '', 1));
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
    // Clear srcs so the previous task's iframe DOM goes away while we
    // re-prepare. The iframe is also key'd on taskId so React/Preact
    // forces a fresh DOM element when this resolves — no chance the new
    // ttyd connection inherits the old WebSocket.
    setTermSrc('');
    setVncSrc('');
    setPhase('preparing');
    setErr('');
    prepareTerminal(taskId)
      .then(() => {
        if (cancelled) return;
        // Only now do we set the src — the iframe renders for the first
        // time on this taskId with the prepare-terminal pending file
        // already on disk. ttyd's entry script reads + consumes it
        // before any concurrent connection can race us.
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

  /**
   * Open this task's terminal in a new tab. The terminal-entry script is
   * one-shot (reads `/tmp/.claude-terminal-pending` then deletes it), so
   * the iframe in the current tab already consumed our previous prepare.
   * We have to re-prepare for the new ttyd connection — otherwise the new
   * tab falls through to a fresh bash shell.
   *
   * window.open() is called synchronously inside the click handler so
   * popup blockers don't kill it; we navigate the pre-opened tab to the
   * terminal URL once prepareTerminal() resolves. Drop 'noopener' so we
   * can hold a reference to the new window, then null win.opener after
   * navigating to preserve the same protection.
   */
  async function openTerminalInNewTab() {
    const win = window.open('about:blank', '_blank');
    if (win) win.opener = null;
    try {
      await prepareTerminal(taskId);
    } catch {
      /* fall through — still open the terminal so user sees an error */
    }
    const url = terminalUrl();
    if (win && !win.closed) {
      win.location.replace(url);
    } else {
      // Popup blocked: best we can do is open in the current tab.
      window.location.href = url;
    }
  }

  async function openPort(e: Event) {
    e.preventDefault();
    const n = parseInt(port, 10);
    if (!n || n < 1 || n > 65535) {
      setPortStatus('Enter a port 1-65535');
      return;
    }
    // Normalize path — empty becomes '/', missing leading slash is added,
    // raw whitespace stripped. Mirrors the server-side validation.
    let normPath = (urlPath || '/').trim();
    if (!normPath) normPath = '/';
    if (!normPath.startsWith('/')) normPath = '/' + normPath;
    setPortBusy(true);
    const target = `localhost:${n}${normPath === '/' ? '' : normPath}`;
    setPortStatus(`→ ${target}…`);
    try {
      const r = await openLocalhostPort(n, normPath);
      if (r && 'error' in r) {
        setPortStatus(`open failed: ${r.error}`);
      } else {
        setPortStatus(`→ ${target}`);
        try {
          localStorage.setItem(LAST_PORT_KEY, String(n));
          localStorage.setItem(LAST_PATH_KEY, normPath);
        } catch { /* noop */ }
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
          <form class="term-pane-port" onSubmit={openPort} title="Open localhost:<port><path> in the in-pod Chrome (right pane)">
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
            <input
              type="text"
              class="term-pane-port-path mono"
              value={urlPath}
              onInput={(e) => setUrlPath((e.target as HTMLInputElement).value)}
              placeholder="/"
              aria-label="Path or query suffix (e.g. /admin or /?dev=1)"
              disabled={portBusy}
              spellcheck={false}
              autocapitalize="off"
            />
            <Button
              type="submit"
              size="sm"
              variant="secondary"
              disabled={portBusy}
              title="Point the in-pod browser at localhost:<port><path>"
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
        <MutatorOnly>
          <input
            ref={uploadInputRef}
            type="file"
            hidden
            onChange={onUploadFile}
          />
          <Button
            size="sm"
            variant="ghost"
            onClick={() => uploadInputRef.current?.click()}
            disabled={uploading || phase !== 'ready'}
            title={
              uploading
                ? 'Uploading…'
                : 'Upload a file into this task\'s workspace and notify the Claude session'
            }
          >
            <Icon name="plus" size={12} /> {uploading ? 'Uploading…' : 'Upload'}
          </Button>
        </MutatorOnly>
        <Button
          size="sm"
          variant={scrollMode ? 'primary' : 'ghost'}
          onClick={toggleScrollMode}
          disabled={scrollBusy || phase !== 'ready'}
          title={
            scrollMode
              ? 'Exit scroll mode — return to the live session'
              : 'Enter scroll mode — arrows / PgUp / wheel scroll the tmux history. Replaces Ctrl+B [ + q.'
          }
        >
          {scrollMode ? 'Exit scroll' : 'Scroll'}
        </Button>
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
          onClick={openTerminalInNewTab}
          title="Open this task's terminal in its own browser tab"
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

      {phase === 'preparing' && (
        <div class="term-pane-loading muted">
          <span class="term-pane-loading-spinner" aria-hidden="true" />
          Attaching to tmux session…
        </div>
      )}
      {phase === 'ready' && (
        <div class={`term-pane-frames ${withVnc ? 'term-pane-frames-split' : ''}`}>
          {/* key={taskId} → guarantees a fresh DOM iframe per task. Without
              this, switching tasks would reuse the previous iframe element
              and we'd race ttyd against a half-torn-down WebSocket. Only
              render once the prepare-terminal POST has resolved (phase='ready')
              so the entry script's pending-file read sees the right value. */}
          {termSrc && (
            <iframe
              key={`term-${taskId}-${termSrc}`}
              class="term-pane-iframe"
              src={termSrc}
              title="Task terminal"
              allow="clipboard-read; clipboard-write"
            />
          )}
          {withVnc && vncSrc && (
            <iframe
              key={`vnc-${taskId}-${vncSrc}`}
              ref={vncRef}
              class="term-pane-iframe term-pane-iframe-vnc"
              src={vncSrc}
              title="Workspace browser (VNC)"
              allow="clipboard-read; clipboard-write"
            />
          )}
        </div>
      )}
    </div>
  );
}
