---
name: kc-screenshot
description: Capture desktop + mobile, dark + light screenshots of the kube-coder dashboard SPA for visual QA of a UI change. Use when the user wants to see a frontend change rendered in a real browser rather than just unit-tested.
user-invocable: true
allowed-tools: Bash, Read
argument-hint: "[output dir] (default: /home/dev/screenshots)"
---

# Screenshot the dashboard SPA

Drives a headless Chromium across desktop (1280x800) and mobile (390x844)
viewports in dark + light themes, against a local dev harness that stubs auth.
Useful for changes that can't be fully unit-tested (terminal iframe, layout,
theming). Read the resulting PNGs with the Read tool — they render inline.

## How it works

- **`charts/workspace/web/dev_server.py <port>`** — a thin harness that imports
  the real `charts/workspace/server.py` and monkeypatches `check_claude_auth`
  to always return True, so the SPA can hit `/api/*` without the OAuth proxy.
  The production dashboard on 6080 is untouched.
- **`charts/workspace/web/scripts/shoot.mjs`** — drives Chromium via
  `playwright-core` and writes `phaseN-<view>-<viewport>-<theme>.png` files.
  Sibling variants exist for specific surfaces: `shoot-hv.mjs` (hypervisor),
  `shoot-msgchat.mjs`, `shoot-docs.mjs`, `shoot-toolbar-paste.mjs`.

## Steps

### 1. Build the SPA (the dev server serves `dist/`)

```bash
yarn --cwd charts/workspace/web build
```

Rebuild after any source edit — the dev server picks up the new `dist/` on the
next request, no restart needed.

### 2. Start the dev server on port 7070

```bash
cd /home/dev/kube-coder
nohup python3 charts/workspace/web/dev_server.py 7070 > /tmp/dev_server.log 2>&1 &
disown
sleep 2
curl -sf http://localhost:7070/ >/dev/null && echo "dev server up" || tail -20 /tmp/dev_server.log
```

### 3. Ensure a Chromium binary is installed

The shoot scripts auto-discover the Playwright Chromium via
`scripts/chromium-path.mjs` (checks `$KC_CHROMIUM`, then the highest-revision
`chromium-<rev>` under any ms-playwright cache root), so you don't hardcode or
symlink anything. Just make sure one is installed:

```bash
ls -d ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null \
  || yarn --cwd charts/workspace/web exec playwright install chromium
```

If it lives somewhere unusual, point the scripts at it explicitly with
`KC_CHROMIUM=/path/to/chrome`.

### 4. Run the shoot script

```bash
OUT="${ARGUMENTS:-/home/dev/screenshots}"
cd /home/dev/kube-coder/charts/workspace/web   # must run where node_modules is
node scripts/shoot.mjs "$OUT"
ls -t "$OUT"/*.png | head
```

Then Read a couple of the output PNGs to visually confirm the change.

## Gotchas

- **Onboarding modal** — a first-run `.ob-scrim` overlay covers the page and
  eats clicks. The shoot scripts suppress it with
  `addInitScript(() => localStorage.setItem('kc.onboardingDone','true'))`. If
  you script a capture by hand, do the same.
- **Theme forcing** — Chromium ignores `prefers-color-scheme` here; the scripts
  set `data-theme="dark"` on `<html>` after navigation so the tokens.css dark
  rules apply. Don't rely on the OS media query.
- **Routes 404 in-browser but 200 via curl** — usually `dev_server.py`'s auth
  monkeypatch went stale against a `server.py` signature change. Check
  `/tmp/dev_server.log` for a `TypeError` in `check_claude_auth`; the stubs
  should swallow `*args, **kwargs`.

## Cleanup

```bash
pkill -f "dev_server.py 7070" 2>/dev/null || true
```
