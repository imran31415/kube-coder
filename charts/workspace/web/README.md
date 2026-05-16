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
`/opt/dashboard-dist/` and `server.py` serves it at `/next/` (with the legacy
dashboard still at `/`). After Phase 6 the new SPA moves to `/` and the legacy
dashboard.html is retired.

## Layout

```
src/
  main.tsx         entry
  app.tsx          temp landing (Phase 0)
  api/             typed fetch wrappers (Phase 1+)
  store/           signals-based state (Phase 1+)
  routes/          page-level components
  components/      Drawer / BottomSheet / BottomNav / CommandPalette / …
  hooks/           useSSE / useSwipe / useShortcut / useMediaQuery
  styles/
    tokens.css     design tokens (dark + light)
    reset.css
    globals.css
```
