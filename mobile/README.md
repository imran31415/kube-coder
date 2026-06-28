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

The app's first screen asks for your **workspace host** (`https://you.kube-coder…`)
and an **API token**. Get the token from your dashboard (sign in, Settings tab),
paste it in — it is stored in the device keychain/keystore via `expo-secure-store`.

### Demo / mock mode

`EXPO_PUBLIC_MOCK=1` skips onboarding and serves fake tasks/memory/metrics so
the UI is fully populated with no backend — used for screenshots and previews.

## Screenshots (store assets)

```bash
npm run screenshots
```

Exports the web build in mock mode and drives it with Playwright/Chromium at
exact App Store / Play Store device pixel sizes, writing PNGs to
`../ios-assets/` and `../android-assets/`. Device matrix lives in
`scripts/screenshots.mjs`.

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
