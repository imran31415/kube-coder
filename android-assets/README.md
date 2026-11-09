# Google Play screenshots

Generated from the kube-coder mobile app running its **demo/mock build**
(`EXPO_PUBLIC_MOCK=1`) — real UI, fake data, no workspace required. Regenerate
with `make mobile-android-screenshots` (from the repo root).

| Folder        | Device class        | Pixels (W×H) | Play requirement                |
|---------------|---------------------|--------------|---------------------------------|
| `phone/`      | Android phone       | 1080 × 1920  | 2–8 phone screenshots required  |
| `tablet-7/`   | Android 7" tablet   | 1216 × 2160  | Optional (recommended)          |
| `tablet-10/`  | Android 10" tablet  | 1620 × 2880  | Optional (recommended)          |

Screens captured per device, in Play display order: `01-desktop`,
`02-task-detail`, `03-board`, `04-chat`, `05-builds`, `06-apps`, `07-memory`,
`08-metrics`.

Play allows at most 8 screenshots per device class and asks for 9:16 portrait.
Upload under Play Console → Store listing → Phone / Tablet screenshots, or push
them (with the listing text) in one shot via `make mobile-play-metadata`
(fastlane supply — it stages `phone/` → `phoneScreenshots`, `tablet-7/` →
`sevenInchScreenshots` and `tablet-10/` → `tenInchScreenshots`). The
source-of-truth dimensions and the 8-screen selection live in
`mobile/scripts/screenshots.mjs`.
