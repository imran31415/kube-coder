# kube-coder — Google Play listing

Human-readable reference for the Play Console "Main store listing" page.
Character limits noted; everything here is within them.

> **Automated push:** the machine source of truth for these fields lives in
> `mobile/fastlane/metadata/android/en-US/` and is pushed with
> `make mobile-play-metadata` (fastlane supply — listing text + screenshots,
> no binary). Edit the `.txt` files there when you change copy, and keep this
> doc in sync for humans. `make mobile-play-metadata-download` pulls the live
> listing back into that folder.

## App details

- **App name** (≤30): `kube-coder`
- **Short description** (≤80):

```
Drive your kube-coder dev workspace: AI coding tasks, live terminal, app previews.
```

- **Full description** (≤4000):

```
kube-coder mobile is the companion app for kube-coder, an open-source,
self-hosted cloud development platform (github.com/imran31415/kube-coder).
If you run a kube-coder workspace, this app lets you drive it from anywhere.

CONNECT IN SECONDS
Point the app at your workspace host and paste the API token from your
dashboard (Settings → Mobile app). No account, no sign-up — the app talks
directly to your own server, and the token is stored securely on your device.

Just looking? Tap "Explore the public demo" on the first screen to browse a
read-only demo workspace — no token needed.

WHAT YOU CAN DO
• Tasks — start a coding task for your AI assistant, follow the live session,
  and send follow-ups when it needs input.
• Live terminal — the task view renders your workspace's real terminal, the
  same session the web dashboard shows.
• Apps — see the dev servers running in your workspace and open them in-app.
  Split the screen to watch your app change while the assistant works on it.
• Desktop — your one-tap launchers (build prompts, URLs, shell commands),
  shared with the web dashboard.
• Memory — browse what your workspace remembers.
• Metrics — live CPU, memory, and disk, plus service health.

PRIVATE BY DESIGN
No accounts. No analytics. No third-party services. The app connects only to
the workspace host you configure, over HTTPS, using your token.

kube-coder itself is open source — read the code, run your own, or try the
demo: github.com/imran31415/kube-coder
```

- **Category**: Tools (or Productivity)
- **Website**: `https://github.com/imran31415/kube-coder`
- **Email**: your support address

## Data safety form

- No data collected, no data shared. The app has no analytics/tracking and
  communicates only with the user-configured server over HTTPS.
- Credentials (the API token) are stored on-device (Android Keystore via
  expo-secure-store) and never leave the device except to the user's server.

## Pre-launch / review access

No credentials needed: tap **"Explore the public demo"** on the first screen —
a hosted read-only demo workspace with every tab populated.

## Screenshots

Upload from the sibling folders (regenerate with `make mobile-screenshots`):

| Folder | Slot |
|---|---|
| `phone/` | Phone (1080×1920) |
| `tablet-10/` | 10" tablet (1600×2560) |

Order: 01-tasks, 02-task-detail, 03-desktop, 04-apps, 05-new-task, 06-memory,
07-metrics.
