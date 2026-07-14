---
name: kc-preflight
description: Run kube-coder's full CI suite locally before pushing — helm lint + unit tests, server.py tests, dashboard SPA build + vitest, and shell syntax checks. Use before opening or updating a PR so CI is green on the first push.
user-invocable: true
allowed-tools: Bash, Read
argument-hint: "[all | web | python | helm | shell] (default: all)"
---

# kube-coder preflight (run CI locally)

Mirrors `.github/workflows/ci.yml` so you catch failures before pushing instead
of waiting on a CI round-trip. Run the subset named by `$ARGUMENTS` (default
`all`). Report a concise PASS/FAIL summary per section at the end.

Run from the repo root (`/home/dev/kube-coder`).

## Prereqs (install once, idempotent)

```bash
export PATH="$HOME/.local/bin:$PATH"
# helm (if missing)
command -v helm >/dev/null || {
  cd /tmp && curl -fsSL https://get.helm.sh/helm-v3.14.4-linux-amd64.tar.gz | tar xz \
    && install -m755 linux-amd64/helm ~/.local/bin/helm; cd -; }
# helm-unittest plugin (if missing)
helm plugin list 2>/dev/null | grep -q unittest \
  || helm plugin install https://github.com/helm-unittest/helm-unittest.git --version v0.5.2
```

## `helm` — lint + template + unit tests

```bash
export PATH="$HOME/.local/bin:$PATH"
helm lint charts/workspace/ -f charts/workspace/tests/test-values.yaml
helm template test-ws charts/workspace/ -f charts/workspace/tests/test-values.yaml > /dev/null
helm unittest charts/workspace/
# controller chart too, if you touched it:
# helm lint charts/workspace-controller/ -f charts/workspace-controller/tests/test-values.yaml
# helm unittest charts/workspace-controller/
```

## `python` — server.py unit + integration tests

```bash
make python-tests
# equivalently: cd charts/workspace && python3 -m unittest discover -s tests -p '*_test.py'
```

## `web` — dashboard SPA typecheck, build, and vitest

CI runs the build (which is `tsc --noEmit && vite build`) then vitest:

```bash
yarn --cwd charts/workspace/web install --frozen-lockfile   # first run only
yarn --cwd charts/workspace/web build      # tsc + vite
yarn --cwd charts/workspace/web test       # vitest run
```

Ignore the `DOMException [NetworkError] … /oauth/terminal/` noise in
TerminalPane tests — happy-dom tries to actually fetch iframe `src`s; it's
harmless and pre-existing. Only the final `Test Files … / Tests …` tally matters.

## `shell` — validate the entrypoint scripts

`start.sh` ships via `{{ tpl (.Files.Get "start.sh") . }}`, so a syntax error
there breaks every pod. Always:

```bash
bash -n charts/workspace/start.sh
```

For the SSH sidecar (embedded in YAML), rendering it through helm already
covers syntax; `helm template … | grep` to eyeball the script if you edited it.

## What CI runs that this does NOT

- **Docker Build Smoke Test** — a full image build (~2 min). Usually not worth
  reproducing locally; rely on CI. If you changed the `Dockerfile`, do build it.
- **Trivy / Security Scanning** — image + dependency CVE scans. CI-only.

Call those out in your summary so the user knows what's still pending after push.

## Summary format

End with a table:

| Section | Result |
|---|---|
| helm lint/template/unittest | ✅ / ❌ |
| server.py tests | ✅ / ❌ |
| SPA build + vitest | ✅ / ❌ |
| shell `bash -n` | ✅ / ❌ |

If everything passes, tell the user it's safe to run **kc-ship-pr**.
