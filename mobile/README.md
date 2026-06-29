# kube-coder mobile

A native iOS + Android app to drive your kube-coder workspace from your phone:
list / create / message / kill Claude tasks, tail their output, and browse
memory and metrics — all over the workspace's Bearer-token API.

Built with **Expo (React Native)** so it can be built and shipped from this
Linux workspace via **EAS cloud builds** (no Mac needed to produce an
uploadable iOS build) and submitted to the App Store / Play Store.

> **Why Expo and not Capacitor?** The original plan wrapped the existing
> dashboard SPA in Capacitor. We pivoted because the requirement is
> `eas build --profile production` → upload to iOS, and EAS is the Expo
> toolchain. EAS Build also compiles iOS in the cloud, which is the only way
> to produce a signed `.ipa` from Linux. The app reuses the workspace's API
> contract and auth model (Bearer token), not the SPA's code.

## Layout

```
mobile/
├── app.config.ts        # Expo config: bundle IDs, icons, splash, scheme
├── eas.json             # EAS build + submit profiles (development/preview/production)
├── App.tsx              # root: boot gate → Onboarding or tab navigator
├── src/
│   ├── api/             # client.ts (Bearer fetch), types.ts
│   ├── store/           # config (host+token, secure storage), hooks
│   ├── mock/            # mock backend for the demo/screenshot build
│   ├── screens/         # Onboarding, Tasks, TaskDetail, NewTask, Memory, Metrics, Settings
│   ├── components/      # shared UI primitives
│   └── theme.ts         # design tokens (mirrors the dashboard's dark theme)
└── scripts/
    └── screenshots.mjs  # captures store-sized screenshots from the web build
```

## Develop

```bash
npm install
npm run typecheck         # tsc --noEmit
npm run web               # run in a browser (react-native-web)
npm start                 # Expo dev server (scan QR with Expo Go on a device)
```

## Connect to a workspace

The host is **fully configurable** — point the app at any kube-coder workspace.
The first screen takes a **host** and an **API token**, both of which the
dashboard hands you: open the workspace in a browser → **Settings → Mobile app**
→ Copy the host and token. The token is stored in the device keychain/keystore
via `expo-secure-store`. The app talks to the workspace's Bearer-token API
(`charts/workspace/templates/ingress-claude-api.yaml` — tasks, memory, metrics,
health on port 6080), never the OAuth-gated browser routes. Tasks, memory and
metrics require the token; `/health` is a public liveness probe.

**Cloud / public host** (the normal case):

```
Host:  https://<your-login>.<your-domain>     # e.g. https://imran.dev.scalebase.io
Token: <copy from that workspace's dashboard → Settings>
```

**Local / minikube / any cluster without a public host** — port-forward the
workspace's API port to your machine, then point the app at localhost:

```bash
make mobile-forward USER=<name>            # kubectl port-forward svc/ws-<name> 6080:6080
```
```
Host:  http://localhost:6080               # iOS Simulator (shares the Mac's network)
       http://<your-Mac-LAN-IP>:6080       # physical device, or Android emulator
Token: kubectl -n <ns> exec deploy/ws-<name> -c ide -- cat /home/dev/.claude-tasks/.api-token
```

> **HTTP is allowed on purpose.** Production hosts are HTTPS (cert-manager), but
> a self-hosted or port-forwarded workspace is plain HTTP on a LAN/localhost
> address. iOS ATS (`NSAllowsArbitraryLoads` + `NSAllowsLocalNetworking`) and
> Android cleartext (`expo-build-properties → usesCleartextTraffic`) are enabled
> in `app.config.ts` so both platforms can reach those hosts.

### Demo / mock mode

`EXPO_PUBLIC_MOCK=1` skips onboarding and serves fake tasks/memory/metrics so
the UI is fully populated with no backend — used for screenshots and previews.

## Screenshots (store assets)

Generating the App Store / Play Store screenshots is a **single, reusable,
deterministic command** — no device, simulator, or live workspace required:

```bash
make mobile-screenshots      # from the repo root  (preferred)
# or, from this directory:
npm run screenshots
```

Output lands in [`../ios-assets/`](../ios-assets) and
[`../android-assets/`](../android-assets), one folder per device class, ready to
upload to App Store Connect / Play Console (each has its own README mapping
folders → store requirements).

### How it works

`scripts/screenshots.mjs` is a small pipeline:

1. **Export** the app's web build in demo mode
   (`EXPO_PUBLIC_MOCK=1 expo export --platform web` → `dist/`), so the UI is
   fully populated from `src/mock/` with no backend.
2. **Serve** `dist/` on a local port.
3. For every entry in the **`DEVICES` matrix**, launch headless Chromium
   (Playwright) at that device's CSS viewport × `deviceScaleFactor` — which
   yields the *exact* pixel size each store expects — then walk the app's
   **screen steps** (tasks → task detail → new task → memory → metrics),
   saving a PNG per screen.

Everything is deterministic apart from relative-time labels ("1m ago"), so
re-running only changes those few frames.

### The variation matrix (single source of truth)

The set of variations lives in one array near the top of
`scripts/screenshots.mjs`. Each row defines one device class:

```js
// device CSS viewport × scale = final PNG pixels (the store-required size)
const DEVICES = [
  { platform: 'ios',     name: 'iphone-6.7', w: 430,  h: 932,  scale: 3 }, // 1290×2796
  { platform: 'ios',     name: 'iphone-6.5', w: 414,  h: 896,  scale: 3 }, // 1242×2688
  { platform: 'ios',     name: 'ipad-12.9',  w: 1024, h: 1366, scale: 2 }, // 2048×2732
  { platform: 'android', name: 'phone',      w: 360,  h: 640,  scale: 3 }, // 1080×1920
  { platform: 'android', name: 'tablet-10',  w: 800,  h: 1280, scale: 2 }, // 1600×2560
];
```

These 5 classes (× 5 screens = 25 images) cover what both stores **require**.
To add another variation — a new device size, or a second pass once a light
theme exists — **add one row** here (and, for a new screen, one step in the
`shots()` function) and re-run the command. `make mobile-screenshots` discovers
everything from this matrix; nothing else changes.

> Tooling note: the script uses Playwright + `serve-handler` (dev dependencies).
> `make mobile-screenshots` runs `npx playwright install chromium` first so the
> headless browser is present in CI.

## Build for the stores (EAS)

Prerequisites (one-time, run while logged in to your Expo account):

```bash
npm i -g eas-cli
eas login
eas init                  # creates the EAS project, writes extra.eas.projectId
```

In CI, pin the project instead of `eas init` by setting `EAS_PROJECT_ID` and
`EAS_OWNER` (read by `app.config.ts`) plus `EXPO_TOKEN` for auth.

Then:

```bash
# Production builds (cloud — iOS is built on EAS macOS workers)
eas build --profile production --platform ios       # → .ipa
eas build --profile production --platform android   # → .aab
eas build --profile production --platform all

# Upload to App Store Connect (TestFlight / review)
eas submit --profile production --platform ios
```

iOS submission additionally requires an **Apple Developer account** ($99/yr).
Fill the placeholders in `eas.json` (`appleId`, `ascAppId`, `appleTeamId`) or
pass them via `eas submit` flags / env. Android submission needs a Play
service-account JSON (`submit.production.android.serviceAccountKeyPath`).

All of the above is also wired into the repo `Makefile` — see
`make help | grep mobile`.
