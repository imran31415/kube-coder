# Google Play screenshots

Generated from the kube-coder mobile app running its **demo/mock build**
(`EXPO_PUBLIC_MOCK=1`) — real UI, fake data, no workspace required. Regenerate
with `make mobile-screenshots` (from the repo root).

| Folder        | Device class        | Pixels (W×H) | Play requirement                |
|---------------|---------------------|--------------|---------------------------------|
| `phone/`      | Android phone       | 1080 × 1920  | 2–8 phone screenshots required  |
| `tablet-10/`  | Android 10" tablet  | 1600 × 2560  | Optional (recommended)          |

Screens captured per device: `01-tasks`, `02-task-detail`, `03-new-task`,
`04-memory`, `05-metrics`.

Play accepts 320–3840 px per side with a max 2:1 aspect ratio; the sizes above
satisfy that. Upload under Play Console → Store listing → Phone / Tablet
screenshots. The source-of-truth dimensions live in
`mobile/scripts/screenshots.mjs`.
