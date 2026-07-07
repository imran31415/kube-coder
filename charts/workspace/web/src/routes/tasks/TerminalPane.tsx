import { useEffect, useRef, useState } from 'preact/hooks';
import { prepareTerminal, terminalUrl, vncUrl, openLocalhostPort, getTaskOutput, scrollTerminal } from '../../api/tasks';
import { proxyUrl } from '../../api/apps';
import { isErrorResponse } from '../../api/client';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { getSessionSignals } from './sessionSignals';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { previewFullscreen } from '../../store/ui';
import { extractUrls } from './terminalUrls';
import { TerminalLinks } from './TerminalLinks';

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
// Device preset + custom dims are sessionStorage: a preset sticks while the
// tab is open (actively testing a viewport across tasks) but every fresh
// visit opens Responsive, so the app fills the pane and reflows as the
// split divider is dragged.
const LAST_DEVICE_KEY = 'kc.previewDevice';
const LAST_CUSTOM_W_KEY = 'kc.previewCustomW';
const LAST_CUSTOM_H_KEY = 'kc.previewCustomH';
// Split-divider position (fraction of the frames row given to the terminal).
// localStorage: pane-size taste is durable, unlike the device preset above.
const SPLIT_RATIO_KEY = 'kc.previewSplit';
const SPLIT_MIN = 0.2;
const SPLIT_MAX = 0.8;

type PreviewMode = 'app' | 'browser';

/** A responsive-preview device preset. Dimensions are CSS pixels at the
 *  device's natural (portrait) orientation — the rotate toggle swaps them.
 *  'responsive' (fill the pane) and 'custom' (user-entered) live outside
 *  this table as special device ids. */
interface DevicePreset {
  id: string;
  label: string;
  w: number;
  h: number;
}
const DEVICE_PRESETS: DevicePreset[] = [
  { id: 'iphone-se', label: 'iPhone SE', w: 375, h: 667 },
  { id: 'iphone-15', label: 'iPhone 15', w: 393, h: 852 },
  { id: 'pixel-7', label: 'Pixel 7', w: 412, h: 915 },
  { id: 'ipad-mini', label: 'iPad mini', w: 768, h: 1024 },
  { id: 'ipad-pro', label: 'iPad Pro 11"', w: 834, h: 1194 },
  { id: 'desktop', label: 'Desktop', w: 1280, h: 800 },
];
// Breathing room (px) kept around a device frame when scaling it to fit
// the preview pane, so the device's drop-shadow isn't flush to the edge.
const DEVICE_STAGE_PAD = 24;

// Mirrors AppEmbed's iframe sandbox: same-origin so cookies/localStorage
// work for typical dev servers, scripts/forms/popups/downloads for normal
// app behaviour, and crucially NO allow-top-navigation so a framed app
// can't bounce the user out of the dashboard.
const APP_FRAME_SANDBOX = 'allow-same-origin allow-scripts allow-forms allow-popups allow-downloads';

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

  // Responsive-preview device: which viewport size the App iframe is sized
  // to. 'responsive' fills the pane (the original behaviour); a preset id or
  // 'custom' constrains the iframe to fixed dimensions and scales-to-fit.
  // Persisted per workspace so the next Preview open inherits the choice.
  const [deviceId, setDeviceId] = useState<string>(() => {
    try { return sessionStorage.getItem(LAST_DEVICE_KEY) ?? 'responsive'; }
    catch { return 'responsive'; }
  });
  const [orientation, setOrientation] = useState<'portrait' | 'landscape'>('portrait');
  const [customW, setCustomW] = useState<string>(() => {
    try { return sessionStorage.getItem(LAST_CUSTOM_W_KEY) ?? '390'; }
    catch { return '390'; }
  });
  const [customH, setCustomH] = useState<string>(() => {
    try { return sessionStorage.getItem(LAST_CUSTOM_H_KEY) ?? '844'; }
    catch { return '844'; }
  });
  // The device frame is centered in this stage and scaled down when it
  // wouldn't otherwise fit; we observe the stage's live size to compute the
  // scale. Only meaningful when a fixed device size is active.
  const stageRef = useRef<HTMLDivElement | null>(null);
  const [stageSize, setStageSize] = useState({ w: 0, h: 0 });

  // Draggable split divider (desktop Preview only). splitRatio is the
  // fraction of the frames row given to the terminal; the app/VNC pane gets
  // the rest. Driven by pointer events on the handle — pointer capture keeps
  // the drag alive over the iframes, and an is-dragging class additionally
  // turns off their pointer-events so neither document swallows the stream.
  const framesRef = useRef<HTMLDivElement | null>(null);
  const [splitRatio, setSplitRatio] = useState<number>(() => {
    try {
      const v = parseFloat(localStorage.getItem(SPLIT_RATIO_KEY) ?? '');
      return v >= SPLIT_MIN && v <= SPLIT_MAX ? v : 0.5;
    } catch { return 0.5; }
  });
  const [splitDragging, setSplitDragging] = useState(false);
  const splitRatioRef = useRef(splitRatio);
  splitRatioRef.current = splitRatio;

  function onSplitPointerDown(e: PointerEvent) {
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    setSplitDragging(true);
  }
  function onSplitPointerMove(e: PointerEvent) {
    if (!splitDragging) return;
    const el = framesRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    if (r.width <= 0) return;
    const f = (e.clientX - r.left) / r.width;
    setSplitRatio(Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, f)));
  }
  function onSplitPointerUp() {
    if (!splitDragging) return;
    setSplitDragging(false);
    try { localStorage.setItem(SPLIT_RATIO_KEY, splitRatioRef.current.toFixed(3)); } catch { /* noop */ }
  }
  function resetSplit() {
    setSplitRatio(0.5);
    try { localStorage.setItem(SPLIT_RATIO_KEY, '0.5'); } catch { /* noop */ }
  }

  // The side-by-side split doesn't fit on phones, so on mobile the Preview
  // shows ONE pane at a time — the live session (terminal) or the app preview
  // — chosen via the segmented control. Defaults to the app so the user lands
  // on what they came to see. Desktop keeps the split.
  const isMobile = useIsMobile();
  const [mobilePane, setMobilePane] = useState<'session' | 'preview'>('preview');

  // Mobile scroll: in scroll mode (tmux copy-mode) touch can't wheel-scroll the
  // ttyd iframe — mobile emits no wheel events, so xterm's wheel->arrow
  // conversion never fires. A transparent overlay (rendered only in scroll mode)
  // captures finger drags here and drives copy-mode server-side. Drag is
  // accumulated and flushed throttled so we don't POST per pixel.
  const scrollMode = sessionSignals.scrollMode.value;
  const scrollTouch = useRef({ lastY: 0, accum: 0, lastSent: 0 });
  const SCROLL_LINE_PX = 16; // px of finger travel per scrolled line
  const SCROLL_SEND_MS = 70; // min gap between scroll POSTs
  const onScrollTouchStart = (e: TouchEvent) => {
    scrollTouch.current.lastY = e.touches[0]?.clientY ?? 0;
    scrollTouch.current.accum = 0;
  };
  const onScrollTouchMove = (e: TouchEvent) => {
    if (!e.touches[0]) return;
    e.preventDefault(); // we own the gesture; don't let the page scroll/zoom
    const st = scrollTouch.current;
    const y = e.touches[0].clientY;
    st.accum += y - st.lastY; // finger down (+) reveals older history => scroll up
    st.lastY = y;
    const now = Date.now();
    if (now - st.lastSent < SCROLL_SEND_MS) return;
    const lines = Math.trunc(st.accum / SCROLL_LINE_PX);
    if (lines === 0) return;
    st.accum -= lines * SCROLL_LINE_PX;
    st.lastSent = now;
    scrollTerminal(taskId, lines > 0 ? 'up' : 'down', Math.min(Math.abs(lines), 40))
      .catch(() => { /* transient — the next gesture retries */ });
  };

  // Persist the preview-source choice per workspace.
  useEffect(() => {
    try { localStorage.setItem(LAST_MODE_KEY, mode); } catch { /* noop */ }
  }, [mode]);

  // Persist the responsive-preview device + custom dimensions (session-only —
  // see the key comments above).
  useEffect(() => {
    try { sessionStorage.setItem(LAST_DEVICE_KEY, deviceId); } catch { /* noop */ }
  }, [deviceId]);
  useEffect(() => {
    try {
      sessionStorage.setItem(LAST_CUSTOM_W_KEY, customW);
      sessionStorage.setItem(LAST_CUSTOM_H_KEY, customH);
    } catch { /* noop */ }
  }, [customW, customH]);

  // Tappable URL strip — refreshed every ~4s from the task's tmux pane.
  // Skipped in Preview (withVnc) mode since the VNC viewer is the primary
  // surface there and the strip would feel redundant.
  const [urls, setUrls] = useState<string[]>([]);
  useEffect(() => {
    if (withVnc) return;
    let cancelled = false;
    async function pull() {
      try {
        const r = await getTaskOutput(taskId, 60);
        if (cancelled) return;
        // The strip is now a collapsed badge (TerminalLinks) so a short list
        // no longer eats the terminal viewport — keep the freshest few.
        // Freshest URL wins (extractUrls iterates bottom-up), which is what
        // the user needs for time-sensitive flows like Claude's oauth flow.
        setUrls(extractUrls(r.output ?? '', 4));
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
      if (isErrorResponse(r)) {
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

  // Resolve the active device into base (portrait) dimensions, then apply
  // orientation. A device of 0×0 (or 'responsive') means "fill the pane".
  const activePreset = DEVICE_PRESETS.find((p) => p.id === deviceId);
  let baseW = 0;
  let baseH = 0;
  if (deviceId === 'custom') {
    baseW = parseInt(customW, 10) || 0;
    baseH = parseInt(customH, 10) || 0;
  } else if (activePreset) {
    baseW = activePreset.w;
    baseH = activePreset.h;
  }
  // Device sizing is a desktop affordance — on a phone the pane is already
  // tiny, so we always fill it there (and hide the control).
  const deviceActive = !isMobile && deviceId !== 'responsive' && baseW > 0 && baseH > 0;
  const [deviceW, deviceH] = orientation === 'landscape' ? [baseH, baseW] : [baseW, baseH];
  // Scale the device frame down to fit the stage (never up past 1:1). Until
  // the stage has been measured, render at 1:1 to avoid a flash of tiny.
  const fit = stageSize.w > 0 && stageSize.h > 0
    ? Math.min(1, (stageSize.w - DEVICE_STAGE_PAD) / deviceW, (stageSize.h - DEVICE_STAGE_PAD) / deviceH)
    : 1;
  const deviceScale = deviceActive ? Math.max(0.1, fit) : 1;

  // Track the stage size so the scale-to-fit recomputes on layout changes
  // (window resize, fullscreen toggle, split reflow). Only attached while a
  // fixed device size is active and the App pane is showing.
  useEffect(() => {
    if (!deviceActive || !showApp) return;
    const el = stageRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r) setStageSize({ w: r.width, h: r.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [deviceActive, showApp]);

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
          {/* Responsive-preview size: pick a device preset (or a custom
              width×height) to constrain the App iframe. Desktop only — the
              mobile pane is already a single small viewport. */}
          {showApp && !isMobile && (
            <div class="term-pane-device" role="group" aria-label="Preview size">
              <select
                class="term-pane-device-select mono"
                value={deviceId}
                onChange={(e) => setDeviceId((e.target as HTMLSelectElement).value)}
                aria-label="Device preset"
                title="Preview the app at a device viewport size"
              >
                <option value="responsive">Responsive</option>
                {DEVICE_PRESETS.map((p) => (
                  <option key={p.id} value={p.id}>{p.label} · {p.w}×{p.h}</option>
                ))}
                <option value="custom">Custom…</option>
              </select>
              {deviceId === 'custom' && (
                <span class="term-pane-device-custom">
                  <input
                    type="number"
                    class="term-pane-device-dim mono"
                    min={50}
                    max={3840}
                    value={customW}
                    onInput={(e) => setCustomW((e.target as HTMLInputElement).value)}
                    aria-label="Custom width (px)"
                  />
                  <span class="term-pane-device-x" aria-hidden="true">×</span>
                  <input
                    type="number"
                    class="term-pane-device-dim mono"
                    min={50}
                    max={3840}
                    value={customH}
                    onInput={(e) => setCustomH((e.target as HTMLInputElement).value)}
                    aria-label="Custom height (px)"
                  />
                </span>
              )}
              {deviceActive && (
                <button
                  type="button"
                  class="term-pane-navbtn"
                  onClick={() => setOrientation((o) => (o === 'portrait' ? 'landscape' : 'portrait'))}
                  title={orientation === 'portrait' ? 'Rotate to landscape' : 'Rotate to portrait'}
                  aria-label="Rotate orientation"
                >
                  ⤢
                </button>
              )}
              {deviceActive && (
                <span class="term-pane-device-readout mono" aria-live="polite">
                  {deviceW}×{deviceH}{deviceScale < 1 ? ` · ${Math.round(deviceScale * 100)}%` : ''}
                </span>
              )}
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

      {!withVnc && (
        <TerminalLinks urls={urls} label="Links from session" />
      )}

      {phase === 'preparing' && (
        <div class="term-pane-loading muted">
          <span class="term-pane-loading-spinner" aria-hidden="true" />
          Attaching to tmux session…
        </div>
      )}
      {phase === 'ready' && (
        // Split only on desktop Preview; mobile shows a single full pane.
        // The inline column template implements the draggable divider —
        // only set while the split class is active so it can't leak into
        // the single-pane layouts.
        <div
          ref={framesRef}
          class={`term-pane-frames ${withVnc && !isMobile ? 'term-pane-frames-split' : ''} ${splitDragging ? 'is-dragging' : ''}`}
          style={withVnc && !isMobile
            ? { gridTemplateColumns: `${splitRatio}fr 6px ${1 - splitRatio}fr` }
            : undefined}
        >
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
          {/* Mobile + scroll mode: a transparent overlay turns finger drags into
              tmux copy-mode scrolling (the ttyd iframe ignores touch). Only
              present in scroll mode, so normal taps/typing reach the terminal
              the rest of the time. */}
          {termSrc && isMobile && scrollMode && (!withVnc || mobilePane === 'session') && (
            <div
              class="term-pane-scroll-overlay"
              onTouchStart={onScrollTouchStart}
              onTouchMove={onScrollTouchMove}
              aria-hidden="true"
            >
              <span class="term-pane-scroll-hint">Drag to scroll · Exit scroll mode to type</span>
            </div>
          )}
          {withVnc && !isMobile && (
            <div
              class="term-pane-split-handle"
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize panes — drag, arrow keys, or double-click to reset"
              aria-valuenow={Math.round(splitRatio * 100)}
              aria-valuemin={SPLIT_MIN * 100}
              aria-valuemax={SPLIT_MAX * 100}
              tabIndex={0}
              title="Drag to resize · double-click to reset"
              onPointerDown={onSplitPointerDown}
              onPointerMove={onSplitPointerMove}
              onPointerUp={onSplitPointerUp}
              onPointerCancel={onSplitPointerUp}
              onDblClick={resetSplit}
              onKeyDown={(e: KeyboardEvent) => {
                if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
                e.preventDefault();
                const next = Math.min(SPLIT_MAX, Math.max(SPLIT_MIN,
                  splitRatio + (e.key === 'ArrowLeft' ? -0.05 : 0.05)));
                setSplitRatio(next);
                try { localStorage.setItem(SPLIT_RATIO_KEY, next.toFixed(3)); } catch { /* noop */ }
              }}
            />
          )}
          {withVnc && showApp && (
            // The iframe stays a single element across responsive↔device
            // switches (the stage/frame wrappers are always present) so the
            // app isn't reloaded just by resizing — only port/path changes,
            // which bump appReloadKey, remount it.
            <div class={`term-pane-device-stage ${deviceActive ? '' : 'is-responsive'}`} ref={stageRef}>
              <div
                class="term-pane-device-frame"
                style={deviceActive
                  ? { width: `${deviceW}px`, height: `${deviceH}px`, transform: `scale(${deviceScale})` }
                  : undefined}
              >
                <iframe
                  ref={appRef}
                  key={`app-${taskId}-${appReloadKey}`}
                  class="term-pane-iframe term-pane-iframe-app term-pane-iframe-device"
                  src={proxyUrl(appPort, appPath)}
                  title="App preview"
                  sandbox={APP_FRAME_SANDBOX}
                  allow="clipboard-read; clipboard-write"
                />
              </div>
            </div>
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
