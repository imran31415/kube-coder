# iOS App Store screenshots

Generated from the kube-coder mobile app running its **demo/mock build**
(`EXPO_PUBLIC_MOCK=1`) — real UI, fake data, no workspace required. Regenerate
with `make mobile-screenshots` (from the repo root).

Each folder is named for the device class and is sized to the exact pixels
App Store Connect expects:

| Folder         | Device class            | Pixels (W×H) | Required? |
|----------------|-------------------------|--------------|-----------|
| `iphone-6.7/`  | iPhone 6.7" Pro Max      | 1290 × 2796  | Yes       |
| `iphone-6.5/`  | iPhone 6.5"              | 1242 × 2688  | One of 6.5/6.7 |
| `ipad-12.9/`   | iPad Pro 12.9"          | 2048 × 2732  | Yes (iPad-enabled app) |

Screens captured per device: `01-tasks`, `02-task-detail`, `03-new-task`,
`04-memory`, `05-metrics`.

Upload these in App Store Connect → your app → the matching display size, or via
`fastlane deliver`. The source-of-truth dimensions live in
`mobile/scripts/screenshots.mjs`.
