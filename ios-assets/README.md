# iOS App Store screenshots

Generated from the kube-coder mobile app running its **demo/mock build**
(`EXPO_PUBLIC_MOCK=1`) — real UI, fake data, no workspace required. Regenerate
with `SCREENSHOT_PLATFORM=ios make mobile-screenshots` (from the repo root).

Each folder is named for the device class and is sized to the exact pixels
App Store Connect expects:

| Folder         | Device class            | Pixels (W×H) | Required? |
|----------------|-------------------------|--------------|-----------|
| `iphone-6.7/`  | iPhone 6.7" Pro Max      | 1290 × 2796  | Yes       |
| `iphone-6.5/`  | iPhone 6.5"              | 1242 × 2688  | One of 6.5/6.7 |
| `ipad-12.9/`   | iPad Pro 12.9"          | 2048 × 2732  | Yes (iPad-enabled app) |

Screens captured per device, in App Store display order: `01-desktop`,
`02-task-detail`, `03-board`, `04-chat`, `05-builds`, `06-apps`, `07-memory`,
`08-metrics`.

For a new version, `make mobile-ios-listing` recaptures and uploads in one step.
It stops before changing anything if the version is already submitted.

Push existing PNGs to the App Store listing with `make mobile-ios-screenshots`
(`mobile/scripts/sync-ios-screenshots.rb`) — it deletes each device's existing
screenshots and uploads exactly these 8, in filename order, via the App Store
Connect API. This deliberately bypasses fastlane `deliver`, whose screenshot
step runs a second verification pass that re-uploads images and leaves
duplicates. Listing **text** goes up separately with `make mobile-metadata`
(deliver, `skip_screenshots`). You can also upload manually in App Store
Connect → your app → the matching display size. The source-of-truth dimensions
live in `mobile/scripts/screenshots.mjs`.
