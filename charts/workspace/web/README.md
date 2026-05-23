# kube-coder dashboard (next)

The next-generation workspace dashboard. Vite + Preact + TypeScript.

## Local dev

Requires Node 20 and yarn 1.22.x.

```
yarn install
yarn dev      # http://localhost:5173, proxies /api → :6080
yarn build    # → dist/
yarn typecheck
```

`yarn dev` proxies API / health / metrics / oauth to a running `server.py` on
port 6080. Start `server.py` first if you want live data.

## Production

`yarn build` produces `dist/`. The image's Dockerfile copies it to
`/opt/dashboard-dist/` and `server.py` serves it at `/` (with `/next/`
kept as a legacy alias).

## Layout

```
src/
  main.tsx         entry
  app.tsx          shell + router
  api/             typed fetch wrappers
  store/           signals-based state
  routes/          page-level components (desktop / tasks / memory / triggers / files / docs / settings)
  components/      Drawer / BottomSheet / BottomNav / CommandPalette / Topbar / …
  hooks/           useSSE / useSwipe / useShortcut / useMediaQuery / useEscape / …
  styles/
    tokens.css     design tokens (dark + light)
    reset.css
    globals.css
```
