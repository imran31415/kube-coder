import { useEffect, useRef, useState } from 'preact/hooks';
import { prepareTerminal, terminalUrl, vncUrl, openLocalhostPort, getTaskOutput } from '../../api/tasks';
import { proxyUrl } from '../../api/apps';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { getSessionSignals } from './sessionSignals';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { previewFullscreen } from '../../store/ui';

export interface TerminalPaneProps {
  taskId: string;
  /** When true, renders ttyd on the left and a preview of the app on the
   *  right. The preview source toggles between the in-app reverse-proxy
   *  iframe ('app') and the in-pod Chrome via noVNC ('browser'). */
  withVnc?: boolean;
}

const LAST_PORT_KEY = 'kc.previewPort';
const LAST_PATH_KEY = 'kc.previewPath';
const LAST_MODE_KEY = 'kc.previewMode';

type PreviewMode = 'app' | 'browser';

// Mirrors AppEmbed's iframe sandbox: same-origin so cookies/localStorage
// work for typical dev servers, scripts/forms/popups/downloads for normal
// app behaviour, and crucially NO allow-top-navigation so a framed app
// can't bounce the user out of the dashboard.
const APP_FRAME_SANDBOX = 'allow-same-origin allow-scripts allow-forms allow-popups allow-downloads';

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

  // Mirror our local phase into the per-task session signal so
  // TaskDetail's status dot in the unified bar can reflect it without
  // prop-drilling.
  const sessionSignals = getSessionSignals(taskId);
  useEffect(() => {
    sessionSignals.phase.value = phase;
  }, [phase, sessionSignals]);
  // Reset scroll-mode state when switching tasks (the server-side tmux
  // state is per session; UI label should reset to match).
  useEffect(() => {
    sessionSignals.scrollMode.value = false;
  }, [taskId, sessionSignals]);

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
  // Preview source: 'app' embeds the reverse-proxy iframe directly (fast,
  // real DOM, no in-pod browser); 'browser' drives the in-pod Chrome and
  // mirrors it via noVNC (for things that need a real browser, e.g. apps
  // that can't be framed). Defaults to 'app' — the lighter, newer path.
  const [mode, setMode] = useState<PreviewMode>(() => {
    try { return localStorage.getItem(LAST_MODE_KEY) === 'browser' ? 'browser' : 'app'; }
    catch { return 'app'; }
  });
  // Committed target for the app iframe. Kept separate from the port/path
  // input so typing doesn't reload the frame on every keystroke — only
  // "Open" (or the initial mount) commits. Seeded from the persisted port.
  const [appPort, setAppPort] = useState<number>(() => {
    try {
      const n = parseInt(localStorage.getItem(LAST_PORT_KEY) ?? '8080', 10);
      return n >= 1 && n <= 65535 ? n : 8080;
    } catch { return 8080; }
  });
  const [appPath, setAppPath] = useState<string>(() => {
    try { return localStorage.getItem(LAST_PATH_KEY) ?? '/'; }
    catch { return '/'; }
  });
  // Bumped to force a fresh iframe mount — browsers don't reliably refetch
  // on a bare src reassignment (same trick AppEmbed uses).
  const [appReloadKey, setAppReloadKey] = useState(0);
  const [portStatus, setPortStatus] = useState<string>('');
  const [portBusy, setPortBusy] = useState(false);
  const vncRef = useRef<HTMLIFrameElement | null>(null);
  const appRef = useRef<HTMLIFrameElement | null>(null);

  // The side-by-side split doesn't fit on phones, so on mobile the Preview
  // shows ONE pane at a time — the live session (terminal) or the app preview
  // — chosen via the segmented control. Defaults to the app so the user lands
  // on what they came to see. Desktop keeps the split.
  const isMobile = useIsMobile();
  const [mobilePane, setMobilePane] = useState<'session' | 'preview'>('preview');

  // Persist the preview-source choice per workspace.
  useEffect(() => {
    try { localStorage.setItem(LAST_MODE_KEY, mode); } catch { /* noop */ }
  }, [mode]);

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
  // Reattach when the per-task counter bumps from outside (settings menu
  // in TaskDetail's unified bar). Skips the initial 0 → 0 case via the
  // dep + a guard ref. Watching the value via useEffect is enough; the
  // signal subscription is implicit.
  const lastReattachAt = useRef<number>(sessionSignals.reattachCounter.value);
  useEffect(() => {
    const cur = sessionSignals.reattachCounter.value;
    if (cur === lastReattachAt.current) return;
    lastReattachAt.current = cur;
    reattach();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionSignals.reattachCounter.value]);

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

  /** Reload just the app-preview iframe (App mode). */
  function reloadApp() {
    setAppReloadKey((k) => k + 1);
    setPortStatus('reloaded');
  }

  // Back/forward drive the app iframe's own history. The app is proxied
  // through our origin, so the iframe is same-origin and contentWindow is
  // reachable; guard anyway in case a navigation lands cross-origin.
  function appBack() {
    try { appRef.current?.contentWindow?.history.back(); } catch { /* cross-origin */ }
  }
  function appForward() {
    try { appRef.current?.contentWindow?.history.forward(); } catch { /* cross-origin */ }
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
    // Remember the target for the next Preview open regardless of mode.
    try {
      localStorage.setItem(LAST_PORT_KEY, String(n));
      localStorage.setItem(LAST_PATH_KEY, normPath);
    } catch { /* noop */ }

    if (mode === 'app') {
      // In-app iframe: no server round-trip — the reverse proxy resolves
      // the port itself. Commit the target + force a fresh mount.
      setAppPort(n);
      setAppPath(normPath);
      setAppReloadKey((k) => k + 1);
      setPortStatus(`→ :${n}${normPath === '/' ? '' : normPath}`);
      return;
    }

    // Browser mode: drive the in-pod Chrome and refresh the noVNC viewer.
    setPortBusy(true);
    const target = `localhost:${n}${normPath === '/' ? '' : normPath}`;
    setPortStatus(`→ ${target}…`);
    try {
      const r = await openLocalhostPort(n, normPath);
      if (r && 'error' in r) {
        setPortStatus(`open failed: ${r.error}`);
      } else {
        setPortStatus(`→ ${target}`);
        refreshVnc();
      }
    } catch (err) {
      setPortStatus(err instanceof Error ? `error: ${err.message}` : 'error');
    } finally {
      setPortBusy(false);
    }
  }

  // Which preview pane is active. Desktop always shows the preview pane (the
  // right half of the split); mobile shows it only when the user hasn't
  // switched to the Session pane.
  const showPreview = !isMobile || mobilePane === 'preview';
  const showApp = showPreview && mode === 'app';
  const showBrowser = showPreview && mode === 'browser';
  const fs = previewFullscreen.value;

  return (
    <div class={`term-pane ${withVnc ? 'term-pane-split' : ''}`}>
      {/* Preview-only control strip. Desktop: source toggle (App | Browser) +
          port form drive the right pane (terminal stays on the left). Mobile:
          a single-pane selector (Session | App | Browser) since the split
          doesn't fit, plus app back/forward/reload and a fullscreen toggle. */}
      {withVnc && (
        <div class="term-pane-controls term-pane-port-floating">
          <div class="term-pane-modeseg" role="group" aria-label={isMobile ? 'Preview pane' : 'Preview source'}>
            {isMobile && (
              <button
                type="button"
                class={`term-pane-modeseg-btn ${mobilePane === 'session' ? 'is-active' : ''}`}
                aria-pressed={mobilePane === 'session'}
                onClick={() => setMobilePane('session')}
                title="Live session (terminal)"
              >
                Session
              </button>
            )}
            <button
              type="button"
              class={`term-pane-modeseg-btn ${showApp ? 'is-active' : ''}`}
              aria-pressed={showApp}
              onClick={() => { setMode('app'); setMobilePane('preview'); }}
              title="In-app view — embeds the app directly through the reverse proxy (fast, real DOM)"
            >
              App
            </button>
            <button
              type="button"
              class={`term-pane-modeseg-btn ${showBrowser ? 'is-active' : ''}`}
              aria-pressed={showBrowser}
              onClick={() => { setMode('browser'); setMobilePane('preview'); }}
              title="Browser — renders the page in the in-pod Chrome, mirrored via noVNC"
            >
              Browser
            </button>
          </div>
          {showPreview && (
            <form
              class="term-pane-port"
              onSubmit={openPort}
              title={mode === 'app'
                ? 'Point the in-app iframe at localhost:<port><path>'
                : 'Open localhost:<port><path> in the in-pod Chrome'}
            >
              <span class="term-pane-port-label mono">localhost:</span>
              <input
                type="number"
                class="term-pane-port-input mono"
                min={1}
                max={65535}
                value={port}
                onInput={(e) => setPort((e.target as HTMLInputElement).value)}
                aria-label="Localhost port to preview"
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
                title={mode === 'app'
                  ? 'Load localhost:<port><path> in the in-app iframe'
                  : 'Point the in-pod browser at localhost:<port><path>'}
              >
                Open
              </Button>
            </form>
          )}
          {showApp && (
            <div class="term-pane-navbtns" role="group" aria-label="App navigation">
              <button type="button" class="term-pane-navbtn" onClick={appBack} title="Back" aria-label="Back">
                <Icon name="chevron-left" size={14} />
              </button>
              <button type="button" class="term-pane-navbtn" onClick={appForward} title="Forward" aria-label="Forward">
                <Icon name="chevron-right" size={14} />
              </button>
              <button type="button" class="term-pane-navbtn term-pane-navbtn-reload" onClick={reloadApp} title="Reload" aria-label="Reload">
                ↻
              </button>
            </div>
          )}
          {showPreview && (
            <button
              type="button"
              class="term-pane-navbtn"
              onClick={() => (previewFullscreen.value = !fs)}
              title={fs ? 'Exit fullscreen — back to kube-coder' : 'Fullscreen app'}
              aria-label={fs ? 'Exit fullscreen' : 'Fullscreen'}
            >
              <Icon name={fs ? 'fullscreen-exit' : 'fullscreen'} size={14} />
            </button>
          )}
          {portStatus && (
            <span class="term-pane-port-status mono" aria-live="polite">
              {portStatus}
            </span>
          )}
        </div>
      )}

      {phase === 'error' && (
        <div class="term-pane-error">
          <strong>Could not attach:</strong> {err}
          <div class="muted" style={{ marginTop: 4, fontSize: 12 }}>
            The task may have ended, or the workspace is missing the ttyd entrypoint.
          </div>
          <div class="term-pane-error-actions">
            <Button
              size="sm"
              variant="primary"
              onClick={reattach}
              title="Re-run prepare-terminal and reload the iframe"
            >
              <Icon name="play" size={12} /> Try again
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={openTerminalInNewTab}
              title="Open this task's terminal in its own browser tab"
            >
              New tab
            </Button>
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
        // Split only on desktop Preview; mobile shows a single full pane.
        <div class={`term-pane-frames ${withVnc && !isMobile ? 'term-pane-frames-split' : ''}`}>
          {/* key={taskId} → guarantees a fresh DOM iframe per task. Without
              this, switching tasks would reuse the previous iframe element
              and we'd race ttyd against a half-torn-down WebSocket. Only
              render once the prepare-terminal POST has resolved (phase='ready')
              so the entry script's pending-file read sees the right value.
              On mobile Preview, the terminal shows only on the Session pane. */}
          {termSrc && (!withVnc || !isMobile || mobilePane === 'session') && (
            <iframe
              key={`term-${taskId}-${termSrc}`}
              class="term-pane-iframe"
              src={termSrc}
              title="Task terminal"
              allow="clipboard-read; clipboard-write"
            />
          )}
          {withVnc && showApp && (
            <iframe
              ref={appRef}
              key={`app-${taskId}-${appReloadKey}`}
              class="term-pane-iframe term-pane-iframe-app"
              src={proxyUrl(appPort, appPath)}
              title="App preview"
              sandbox={APP_FRAME_SANDBOX}
              allow="clipboard-read; clipboard-write"
            />
          )}
          {withVnc && showBrowser && vncSrc && (
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
