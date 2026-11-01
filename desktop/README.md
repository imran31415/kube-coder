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
  are allowed because react-native-web emits them.

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
under `/home/dev`, then prints the environment to build and run with.

### CI

`.github/workflows/desktop.yml` runs the Rust unit tests, then builds unsigned
installers on `ubuntu-latest` (`.deb`, `.AppImage`), `macos-latest` (universal
`.dmg`) and `windows-latest` (`.msi`, NSIS `.exe`) for every PR that touches
`desktop/` or `mobile/`, fails if an installer is over 20 MB, and uploads them
as workflow artifacts.

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

Method used for the Linux number below:

1. Build the demo UI and a release binary:
   `KC_DESKTOP_DEMO=1 npx tauri build --no-bundle`.
2. Run it on a virtual display, at the default 1280×820 window, and leave it
   idle for 10 minutes.
3. Sum the RSS of the app's process tree:
   `scripts/measure-rss.sh <pid>` (walks the children and adds VmRSS).

RSS counts shared library pages in every process that maps them, so the sum
over-counts what the app really costs the machine; `scripts/measure-rss.sh`
also prints the PSS total (shared pages divided among their users), which is
the fairer number.

MEASUREMENTS_PLACEHOLDER
