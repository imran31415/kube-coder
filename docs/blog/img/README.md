# Screenshots for `kube-coder-story.md`

Drop each image in this folder using the **exact filename** below — the article already
references these paths. PNG preferred; the article doesn't care about exact dimensions, but
the desktop shots look best around ~1400px wide and the mobile shots around ~400–500px wide.

The shots are numbered in the order they appear in the article.

## The "day on one workstation" flow

| # | Filename | What to capture |
|---|---|---|
| 01 | `01-build-desk.png` | **Desktop.** Build view: session list on the left, a live Claude session streaming in the **Terminal** tab on the right. |
| 02 | `02-apps-preview.png` | **Desktop.** The **Apps** tab previewing a running dev server (App mode), with the **App / Browser** toggle visible. |
| 03 | `03-mobile-waiting-badge.png` | **Mobile.** The dashboard on a phone with the **WaitingBadge** pulsing in the topbar; ideally the browser tab title showing `(1)`. |
| 04 | `04-mobile-task-sheet.png` | **Mobile.** The task-detail **bottom sheet** — Claude's waiting prompt and the **Send-message** box below the live terminal. |
| 05 | `05-mobile-url-and-browser.png` | **Mobile.** The extracted **tappable URL buttons** above the terminal, and/or the app preview rendered in **Browser mode**. |
| 06 | `06-memory-graph.png` | **Desktop or mobile.** The **Memory graph** (D3 force-directed view) showing remembered decisions and their relations. |

## Feature / architecture shots

| # | Filename | What to capture |
|---|---|---|
| 07 | `07-architecture-diagram.png` | A diagram of the request path (browser → oauth2-proxy → ingress → Service → Pod) with the in-pod services called out. *(Diagram, not a screenshot.)* |
| 08 | `08-dashboard-desktop.png` | **Desktop.** The full dashboard — Rail, Build session list, and a session detail pane with its Terminal / Preview / Send-message tabs. |
| 09 | `09-app-proxy-toggle.png` | **Desktop.** A stock SPA (e.g. a Vite app) previewed through the proxy, with the **App / Browser** preview toggle highlighted. |
| 10 | `10-memory-list.png` | **Desktop.** The **Memory** route in list view, with the **History** and **Relations** tabs open on one entry. |
| 11 | `11-triggers.png` | **Desktop.** The **Triggers** route — a webhook and a cron side by side, ideally with a recently-fired build linked from one. |
| 12 | `12-responsive-flip.png` | **Side by side.** Desktop (with the Rail) next to mobile (with the BottomNav), showing the 720px layout flip. |
| 13 | `13-new-build-flow.png` | **Desktop or mobile.** The **New build** flow — assistant picker, working directory, session name. |

## Notes

- If you skip a shot, the article still renders — the `alt` text and italic caption stay visible.
- Want a different file format (`.jpg`, `.webp`) or naming scheme? Tell me and I'll update the references in one pass.
