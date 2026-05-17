# Browser & VNC

> **What this is.** A real Firefox / Chromium running on the pod's
> virtual X display (`:99`, 1280x720), surfaced as a noVNC viewer
> inside the dashboard. Use it for OAuth flows, preview your local
> dev server visually, or let Claude drive a real browser via
> Playwright.

## Quick path

1. **Settings → Browser → Launch Firefox** (or Chrome — whichever is
   installed in your image).
2. **Settings → Browser → Connect VNC** — the viewer iframe opens.
3. Click into the iframe; type, scroll, navigate as normal. The
   `?resize=scale` flag stretches the 1280x720 display to fit.

## From the terminal

```bash
export DISPLAY=:99
firefox &
# or
chromium-browser &
```

The dashboard's VNC view will show whatever is on display `:99`.

## Kiosk mode for previewing local apps

The **Preview** button (when present) calls `POST /api/open-localhost`
with a port number. It launches Chrome in `--app` + `--start-fullscreen`
mode pointed at `http://localhost:<port>` — no chrome, no tabs, just
your app. Used by the dashboard to preview dev servers without you
typing the URL.

## Claude + Playwright

The `playwright/mcp` server is seeded in the Claude config, so any
Claude task can drive Firefox or Chromium via MCP tools
(`browser_navigate`, `browser_click`, `browser_snapshot`, etc.). The
managed Playwright Firefox lives at a known PID — don't `pkill -f
firefox` from a script, or you'll kill it.

> :::scenario
> **Pattern: visual QA of a Vite dev server.**
> 1. `yarn dev` in your project (terminal).
> 2. Click Preview at port 5173 (Settings → Browser).
> 3. Ask a Claude task to walk the app, take screenshots, and report
>    UI bugs. The screenshots land under `/home/dev/screenshots/`.
> :::

## Limits

- **Single virtual display.** You can run multiple browser windows
  but only one display — overlapping windows is messy.
- **No audio.** The pod has no sound device.
- **GPU acceleration is off.** Chrome runs with `--disable-gpu`.
  Heavy WebGL pages render but slowly.
- **Resolution is fixed at 1280x720.** The viewer scales — try
  zooming the viewer rather than changing display resolution.

## Troubleshooting

- **"VNC Connection Error"** — the noVNC server (port 6081) didn't
  start, or you haven't launched a browser yet. Launch one, then
  reconnect.
- **Black screen** — the browser process died. Check `ps aux | grep
  firefox` from the terminal.
- **"Display not available"** — `Xvfb` isn't running. Restart the pod
  (rare; the entrypoint script normally keeps it up).
