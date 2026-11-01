# kube-coder desktop (macOS, Windows, Linux)

A low-footprint desktop client for a kube-coder workspace, built with
[Tauri 2](https://tauri.app). Like the web dashboard and the phone app it is
**only a client**: it talks to a remote workspace over the existing API and
runs nothing locally. It sits in the tray, shows what needs you, raises native
notifications, badges the dock and opens straight into a review or a Build.

Phase 1 of [#833](https://github.com/imran31415/kube-coder/issues/833).
Signing, notarization, auto-update and release publishing are Phase 2.

## Architecture

```
┌──────────────────────────── desktop app ─────────────────────────────┐
│ main window (OS webview: WKWebView / WebView2 / WebKitGTK)           │
│   mobile/'s web export, bundled — the phone UI + a wide layout       │
│   CSP: no network at all, only IPC                                   │
│        │ invoke(kc_request, kc_secret_*, …)   ▲ events (kc://…)      │
│        ▼                                       │                     │
│ Rust core (src-tauri/src)                                            │
│   http.rs    the ONLY place the token is attached; host-scoped       │
│   core.rs    holds GET /api/events (SSE) with the Bearer header,     │
│              Feed alerts + badge, fallback polling, sleep policy     │
│   secrets.rs token ↔ OS keychain        settings.rs  host, prefs     │
│   embed.rs   workspace apps/terminal in separate, IPC-less windows   │
│   tray.rs    tray, dock/taskbar badge                                │
└───────────────────────────────┬───────────────────────────────────────┘
                                │ HTTPS + Bearer, one host only
                                ▼
              workspace (ingress-claude-api.yaml, Bearer ingress)
```

**One client codebase.** The UI is not a fork: `scripts/build-ui.mjs` runs
`expo export --platform web` in `../mobile` at build time and Tauri bundles
the result. The mobile code notices Tauri's injected bridge at runtime
(`mobile/src/desktop/bridge.ts`) and switches in a few well-defined places:

| Concern | Phone | Desktop |
|---|---|---|
| Token storage (`mobile/src/store/storageBackend.ts`) | expo-secure-store | OS keychain via `kc_secret_*` |
| Requests (`mobile/src/api/transport.ts`) | `fetch` + Bearer | `kc_request` → Rust adds the Bearer |
| Live updates (`mobile/src/store/liveEvents.ts`) | polling | Rust holds the SSE stream; polling screens go event-driven |
| Notifications | Expo push from the server | local, from the Feed, same rules as push |
| Layout (`mobile/src/util/wideLayout.ts`) | drawer | sidebar + content pane at ≥ 960 px |
| Workspace apps / terminal | in-screen WebView | separate window with no IPC |
| Deep links | push tap | `kubecoder://` + notification click, same router |

### Screens load on first open

The export is split: `mobile/src/screenRegistry.ts` gives every screen but the
home screen and Onboarding a literal `import()`, which Metro turns into a chunk
of its own, and the navigator renders it through `React.lazy`
(`mobile/src/components/LazyScreen.tsx`). The window loads the shell and the
home screen; a screen's code is read and parsed the first time it is opened,
including by a deep link or a notification click. On iOS and Android Metro
keeps those imports inside the one native bundle, so a phone build is
unchanged.

Expo loads one more chunk up front, `__common`: the modules two or more lazy
screens share (expo-audio, the image picker, the clipboard, Markdown). Those
are only defined there, not run, until a screen that uses them opens.

Two other things keep the web build small. The icons are imported from
`@expo/vector-icons/Ionicons`: the package's barrel import bundled the glyph
maps of all 15 icon families (~470 KB of JS) and their 19 font files (4 MB).
And `mobile/src/push/notifications.web.ts` stands in for the
expo-notifications glue on web, where no Expo push token can be minted (the
desktop app raises its own notifications from the core).

### Live updates

`EventSource` cannot send an `Authorization` header, which is why the phone
polls (see the header of `mobile/src/api/client.ts`). The Rust core can: it
holds `GET /api/events` (`charts/workspace/handlers/workspace.py`) with the
Bearer token, parses it (`sse.rs`) and forwards each named event to the UI.
While the stream is up, the screens whose data the server announces —
Feed, Board, Builds, Mission Control, Security — stop their interval and
refresh on a matching event, plus a 5-minute safety poll (`usePolling(fn, ms,
['feed.'])`). The moment the stream drops they are back on their interval, and
the core re-reads the Feed on a backoff so alerts keep working. Screens whose
data has no event (a Build's output, metrics, the desktop dock) keep polling.

### Notifications and the badge

The phone hears about waiting work through push; the server decides what to
push (`charts/workspace/push_notify.py`). The desktop app has no push channel,
so `feed.rs` makes the same decision over `/api/feed`:

- **what alerts:** `waiting` rows and `decision` rows (`should_push`);
- **coalescing:** the Feed keeps re-emits of one alert on one row; a repeat
  alerts only once that row was read since, or when it is a different row,
  never more often than `KC_PUSH_MIN_INTERVAL` (default 1800 s, same parsing);
- **staleness:** nothing older than an hour, like push's TTL;
- **first look:** opening the app on a workspace records what is already
  there without alerting. The ledger persists per host in the app data dir.

`/api/feed` deliberately does not expose `dedupe_key` (`feed_test.py`), so the
coalescing key is the row's primary ref (`board:<board>:<item>`, `task:<id>`,
…), which is what the server's keys encode.

Clicking a notification opens its target through the same router a phone push
tap uses (`handleNotificationTap`), which also marks the Feed row read.

The badge is the count of `waiting && !read` Feed rows — the same rows and
rule as the phone's drawer badge and Needs-you count, across every kind
(a Build waiting for input needs you as much as a review). It shows as the
dock badge (macOS), the launcher badge (Linux desktops with the Unity launcher
API), a red overlay dot on the taskbar button (Windows has no numeric badge
for desktop apps) and the tray tooltip.

### Sleeping workspaces (#728)

- The core only ever asks the keeper (`/api/keeper/status`, served by the
  keeper, never the pod) and reads the keeper's `workspace_sleeping` answer.
  It never calls `/api/keeper/wake`; only your tap on the wake banner does.
  A launch never wakes a workspace.
- The keeper counts any held connection to the workspace as a dashboard user
  (`keeper_idle.py`), so an always-open app holding the stream would keep a
  keeper-managed workspace awake forever. On a workspace **with** a keeper the
  core lets go — no stream, no polling — once the window has been unfocused
  for 20 minutes (`KC_DESKTOP_RELEASE_AFTER_MIN`), and resumes the moment you
  focus it. The trade-off: an alert raised while you were away arrives when
  you come back. On a workspace **without** a keeper nothing sleeps, so the
  stream is held for as long as the app runs.

### Closing the window

Closing the window does not quit: the app stays in the tray, and the core
keeps the stream, the alerts and the badge going. The webview is nearly all of
the app's memory and the core needs none of it to watch the Feed, so a closed
window gives it back:

- **Linux and Windows** destroy the window, and the webview with it, at once
  (`close_to_tray` in `lib.rs`).
- **macOS** hides the window, because a Mac app that stays in the dock is
  expected to come back as it was left, and destroys it once it has stayed
  hidden for `KC_DESKTOP_UNLOAD_AFTER_MIN` minutes (default 10; `0` drops it on
  close like the other two, `off` keeps it for as long as the app runs). The
  timing is `unload.rs`. The dock badge belongs to the application, not to a
  window (`NSApp.dockTile`), so the core keeps setting it with no window at
  all; a click on the dock icon rebuilds the window.

Opening it again — from the tray, the dock, a notification or a link —
rebuilds it. The UI comes back on the screen it was on: it saves its
navigation state in the core's memory-only `kc.session.*` items
(`mobile/src/desktop/navSession.ts`, `settings.rs`), never on disk, so a fresh
launch still starts on the home screen. Whatever opened the window (a
notification, a link) waits in the core's target queue and lands on top.
Quit is in the tray menu.

### Deep links and single instance

`kubecoder://` links use the push target format, in two spellings:

```
kubecoder://board/<boardId>/<itemId>     kubecoder://board:<boardId>:<itemId>
kubecoder://task/<id>                    kubecoder://task:<id>
kubecoder://thread/<id>                  kubecoder://thread:<id>
kubecoder://scan/<id>                    kubecoder://feed
```

A second launch focuses the running window and forwards its link
(`tauri-plugin-single-instance`). A link that arrives before the UI is ready
waits in the core's queue, and the mobile cold-start parking from #709 holds
it until the navigator is mounted.

## Security model

- **Minimal capabilities.** One capability (`capabilities/main.json`), for the
  `main` window only: event listening and the app's own commands. No shell, no
  filesystem, no HTTP plugin, no opener. `build.rs` lists every command so
  each one needs an explicit grant, and every command checks again that its
  caller is the `main` window.
- **HTTP is scoped in Rust.** The webview cannot open a socket (its CSP allows
  only IPC). `kc_request` takes a *path*; `scope.rs` builds the URL from the
  connected host alone and refuses anything that could leave it (scheme or
  authority in the path, `//`, backslashes, dot segments, fragments, control
  characters). Hosts follow `mobile/src/util/urlPolicy.ts` `validateHost`:
  HTTPS, or plain HTTP to loopback only in a build made with
  `KC_ALLOW_CLEARTEXT=1`. Redirects are never followed. Only benign headers
  pass from the webview (`Accept`, `Content-Type`, upload names); `Authorization`,
  `Cookie`, `Host` and `Origin` are the core's.
- **The token** lives in the OS keychain (`keyring`: macOS Keychain, Windows
  Credential Manager, Linux Secret Service) and in the core's memory. It is
  never written to a file or localStorage, never logged (`Token` redacts
  itself in `Debug`/`Display`), and never handed back to the webview: a read
  returns a mask with the last four characters, which is all Settings shows.
- **No IPC for remote content.** The bundled UI is the only content with IPC.
  Workspace apps and the terminal open in separate windows (`embed.rs`): the
  core makes the `/api/claude/apps/session` bootstrap request itself, gives
  the `kc_app_session` cookie to the new window and points it at the app.
  Those windows match no capability, are pinned to the workspace origin and
  may not open pop-ups.
- **Strict CSP** on the bundled UI: `default-src 'self'`, `script-src 'self'`,
  `connect-src ipc: http://ipc.localhost`, `frame-src 'none'`,
  `object-src 'none'`, `base-uri 'none'`, `form-action 'none'`. Inline styles
  are allowed because react-native-web injects its styles at runtime; Tauri is
  told not to add hashes to `style-src` (`dangerousDisableAssetCspModification:
  ["style-src"]`), since any hash there makes the webview ignore
  `'unsafe-inline'`. Scripts keep Tauri's hashes and no inline script runs.

## Building

The UI build needs Node 20; the core needs a stable Rust toolchain.

```bash
cd desktop
npm ci                 # the Tauri CLI
npx tauri build        # exports ../mobile's web build, then bundles
```

Flags for `scripts/build-ui.mjs` (read by `tauri build` and `tauri dev` too):

| Variable | Effect |
|---|---|
| `KC_DESKTOP_DEMO=1` | the demo-data build (no workspace needed) |
| `KC_ALLOW_CLEARTEXT=1` | allow `http://localhost` workspaces; also enables `KC_DESKTOP_HOST` + `KC_DESKTOP_TOKEN` to pre-seed a connection (memory only) for a test run |

At run time, `KC_DESKTOP_TRACE=1` logs each command the UI calls (its name and
window, never its arguments), `KC_DESKTOP_RELEASE_AFTER_MIN` and
`KC_PUSH_MIN_INTERVAL` tune the sleep policy and the alert throttle,
`KC_DESKTOP_UNLOAD_AFTER_MIN` sets when macOS drops a hidden window's webview,
and `KC_DESKTOP_WEBKIT_DEFAULTS=1` skips the WebKitGTK cache settings (to
measure what they save).

### macOS

Xcode command-line tools. `npx tauri build --bundles dmg --target
universal-apple-darwin` (after `rustup target add aarch64-apple-darwin
x86_64-apple-darwin`) makes one universal `.dmg`. Unsigned builds need a
right-click → Open the first time; signing is Phase 2.

### Windows

The MSVC build tools and WebView2 (preinstalled on Windows 10/11).
`npx tauri build --bundles msi,nsis`.

### Linux

```bash
sudo apt-get install libwebkit2gtk-4.1-dev libayatana-appindicator3-dev \
  librsvg2-dev libdbus-1-dev patchelf
npx tauri build --bundles deb,appimage
```

At runtime the token needs a Secret Service (GNOME Keyring or KWallet). Without
one the app still connects, but only for that session, and asks again next
launch.

In a kube-coder workspace pod `sudo` may be unavailable (the pod runs with
`allowPrivilegeEscalation: false`); `scripts/rootless-linux-deps.sh` downloads
the same packages with `apt-get download` and unpacks them into a prefix
under `/home/dev`, patches WebKitGTK's compiled-in helper path to point into
it, and prints the environment to build and run with (on a GPU-less virtual
display, including `WEBKIT_DISABLE_DMABUF_RENDERER=1`). To try the app with no
workspace, `scripts/fake-workspace.py` serves enough of the API (and counts
requests), and `scripts/fake-notifyd.py` stands in for a notification daemon
and clicks what it is sent.

### CI

`.github/workflows/desktop.yml` runs the Rust unit tests, then builds unsigned
installers on `ubuntu-latest` (`.deb`, `.AppImage`), `macos-latest` (universal
`.dmg`) and `windows-latest` (`.msi`, NSIS `.exe`) for every PR that touches
`desktop/` or `mobile/`, fails if an installer is over 20 MB, and uploads them
as workflow artifacts. The AppImage is reported but not held to 20 MB: it
carries its own WebKitGTK and GTK stack (its AppDir is ~210 MB before
compression), where the `.deb` uses the system's.

## Tests

```bash
cd desktop/src-tauri && cargo test     # host scope, SSE parsing, Feed alert rules, …
cd mobile && npx vitest run            # wide layout, storage backends, deep links, transport
```

## Measuring memory

The #833 budget is **< 120 MB total RSS idle**, counting every process the app
owns: the app itself plus the webview's helper processes (WebKitWebProcess and
WebKitNetworkProcess on Linux, the WebContent/Networking XPC services on macOS,
the msedgewebview2 tree on Windows).

### Linux

1. Build a release binary that may talk to a loopback workspace:
   `KC_ALLOW_CLEARTEXT=1 npx tauri build --no-bundle`.
2. Start `scripts/fake-workspace.py`, and run the app against it
   (`KC_DESKTOP_HOST=http://127.0.0.1:6099 KC_DESKTOP_TOKEN=test-token`) on a
   virtual display at the default 1280×820 window: one workspace connected,
   the event stream held, opened with `kubecoder://feed`, then
   `kubecoder://board/demo/item-1` (the Feed and Board screens visited).
3. For the tray-only state, close the window 70 s in.
4. Leave it idle for 10 minutes, then sum its process tree with
   `scripts/measure-rss.sh <pid>`.

RSS counts shared library pages in every process that maps them, so the sum
over-counts what the app costs the machine. The script also prints PSS
(shared pages divided among their users), USS (pages only this app maps) and
private *dirty* memory, the part the kernel cannot simply drop.

### macOS

`scripts/measure-macos.sh` sums the app and the WebKit XPC services it is
responsible for. Those are started by launchd, so their parent pid is 1 and
`ps` cannot tell them from Safari's; the script asks macOS which app each one
belongs to, the attribution Activity Monitor uses. It prints RSS (what `ps`
shows) and the physical footprint (Activity Monitor's Memory column):

```bash
# connect a workspace, open the Feed and a Board, leave it idle 10 minutes
desktop/scripts/measure-macos.sh
# close the window (⌘W), wait 11 minutes so the webview is dropped
desktop/scripts/measure-macos.sh
```

### Linux, measured in a kube-coder pod (Xvfb, no GPU), 2026-11-01

Same method, same pod, same session, before (`main` at a350d1e) and after
this change:

| State | Build | RSS sum | PSS | USS | private dirty |
|---|---|---:|---:|---:|---:|
| Window open, idle 10 min | before | 423.7 MB | 278.6 MB | 190.9 MB | — |
| Window open, idle 10 min | after | 412.4 MB | 265.3 MB | 175.4 MB | 116.2 MB |
| Tray only (window closed), idle 10 min | before | 211.2 MB | 156.1 MB | 122.1 MB | — |
| Tray only (window closed), idle 10 min | after | 207.5 MB | 152.3 MB | 118.0 MB | 48.5 MB |

PSS depends on what else maps the same libraries at the time, so compare rows
measured together; #835's Phase 1 numbers (437.5 MB RSS window open, 209.7 MB
tray only) were taken on another day.

Where the window-open memory goes (the after build, per process):

- **The web process** (WebKitWebProcess) holds ~65 MB of private dirty
  memory; the rest of its ~200 MB RSS is WebKitGTK, JavaScriptCore and Mesa
  library pages. The JavaScript heap is a small part of it: the same UI in
  Chromium holds ~8 MB of JS heap after visiting the Feed and Board (9.6 MB
  before this split). Splitting the bundle saved ~11 MB here; most of the rest
  is WebKit itself.
- **The app process** maps WebKitGTK's UI-process half, GTK and, on a display
  with no GPU, Mesa's software rasterizer (`libLLVM`, ~42 MB resident in each
  process that draws). Its own heap (the Rust core, GTK) is ~20 MB.
- **The network process** (WebKitNetworkProcess) stays for as long as the app
  runs, ~20 MB of it private, though every request goes through the core.

So with the window open the Linux number stays well over 120 MB. Closing it
(Linux, Windows) or leaving it hidden for 10 minutes (macOS) drops the web
process, which is where the app spends most of its idle life.

**Windows is not measured** — this pod cannot run it. WebView2 has its own
process model; measure it with Task Manager over the app and its
`msedgewebview2` children.

Further levers, not taken here: WebKitGTK's memory-pressure settings must be
given when the web context is created, which wry does without exposing them;
and the network process would need a WebKit API to stop it while no page is
open.
