# kube-coder — App Store Connect listing

Human-readable reference for the App Store Connect listing. Character limits
noted; everything here is within them. Keep it simple — the app is a thin
client, and the listing should read that way for review.

> **Automated push:** the machine source of truth for these fields lives in
> `mobile/fastlane/metadata/` and is pushed with `make mobile-metadata`
> (fastlane deliver — metadata + screenshots, no binary). Edit the `.txt`
> files there when you change copy, and keep this doc in sync for humans.
> `make mobile-metadata-download` pulls the live listing back into that folder.

## App information

- **Name** (≤30): `kube-coder`
- **Subtitle** (≤30): `Client for your dev workspace`
- **Bundle ID**: `app.kubecoder.mobile` (matches `mobile/app.config.ts`)
- **Primary category**: Developer Tools
- **Copyright**: `© 2026 Imran Hassanali`
- **Support URL**: `https://github.com/imran31415/kube-coder`
- **Marketing URL** (optional): `https://github.com/imran31415/kube-coder`

## Promotional text (≤170)

```
Drive your kube-coder workspace from your phone: start AI coding tasks, watch
the live terminal, preview running apps, and reply when your assistant needs
you.
```

## Description (≤4000)

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
  and send follow-ups when it needs input. Active work up front; finished
  tasks tucked under Done.
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

## Keywords (≤100 chars)

```
kubernetes,dev,workspace,terminal,claude,ai,coding,remote,self-hosted,devops
```

## What's New (first release)

```
First release: connect to your kube-coder workspace, run AI coding tasks with
a live terminal, preview running apps, launch from your Desktop, and watch
workspace metrics.
```

## App Review notes (paste into "Notes" in App Review Information)

```
kube-coder mobile is a client for the open-source kube-coder platform
(https://github.com/imran31415/kube-coder). Users self-host the server and
connect with their own host + API token.

TO REVIEW WITHOUT ANY SETUP: on the first screen, tap "Explore the public
demo". It connects to our hosted read-only demo workspace — no account,
credentials, or configuration required. All tabs (Tasks, Desktop, Apps,
Memory, Metrics) are populated in the demo.

The app creates no accounts, collects no data, and contains no purchases.
```

## Privacy (App Privacy section)

- **Data collection**: None. The app has no analytics or tracking and talks
  only to the user's own server.
- Declare "Data Not Collected" across the board.

## Screenshots

Upload from the sibling folders (regenerate with `make mobile-screenshots`):

| Folder | Slot |
|---|---|
| `iphone-6.7/` | iPhone 6.7" (1290×2796) |
| `iphone-6.5/` | iPhone 6.5" (1242×2688) |
| `ipad-12.9/` | iPad Pro 12.9" (2048×2732) |

Order: 01-tasks, 02-task-detail, 03-desktop, 04-apps, 05-new-task, 06-memory,
07-metrics.
