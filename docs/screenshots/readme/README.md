# README screenshots

Every image in this folder is generated from mock data, so no real workspace,
user or secret appears in it. Regenerate after a visual change:

```bash
# Dashboard (Chat, Mission Control, Home, Builds) — 1440×900 @2x
yarn --cwd charts/workspace/web build
python3 charts/workspace/web/dev_server.py 7070 &
node charts/workspace/web/scripts/shoot-readme.mjs docs/screenshots/readme

# Admin console (workspace list, edit limits, phone layout)
make controller-web
node charts/workspace-controller/web/scripts/shoot-readme.mjs docs/screenshots/readme
```

`mobile-app.png` is three iPhone 6.7" frames from the mobile app's mock web
build (`make mobile-screenshots` produces the same screens for the stores),
placed side by side on the slate background.

Both scripts need a Playwright Chromium; set `KC_CHROMIUM` to its binary if it
isn't found automatically.
