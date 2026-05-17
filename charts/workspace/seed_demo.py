#!/usr/bin/env python3
"""Idempotent seed script for the public read-only demo deployment.

Run from the workspace-entrypoint on pod boot whenever READONLY_MODE=true.
Populates a small, representative dataset so visitors landing on the demo
see populated Memory and Build pages instead of empty states.

Idempotent guards: every write is skipped if the destination already
contains data. The demo PVC survives pod restarts, so this script effectively
runs once and then no-ops on every subsequent boot. Safe to run on any pod
(running it on a real workspace is a no-op as long as the stores have
existing data, which they always do).
"""
from __future__ import annotations

import json
import os
import sys
import time

# Match server.py's import path. /tmp/browser is where workspace-entrypoint
# copies the memory package alongside server.py.
sys.path.insert(0, '/tmp/browser')

TASKS_DIR = '/home/dev/.claude-tasks'

# (namespace, key, value, kind, tags, importance)
DEMO_MEMORIES = [
    (
        'user', 'role',
        'Visitor exploring the kube-coder dashboard from the public demo at '
        'demo-public.dev.scalebase.io. Read-only — every mutation endpoint '
        'returns 403. Spin up a personal workspace from the OSS repo to get '
        'a real, writable copy.',
        'semantic', 'demo,user', 0.8,
    ),
    (
        'user', 'preferences',
        'Prefers concise responses with file:line references over long prose '
        'explanations. Wants the code change shown, not a recap of the diff.',
        'procedural', 'demo,user,style', 0.6,
    ),
    (
        'project', 'name',
        'kube-coder — a Kubernetes-native development environment that gives '
        'each user an isolated pod running code-server (VS Code in browser), '
        'a dashboard SPA, tmux-backed Claude/OpenCode task sessions, and a '
        'persistent memory store. Single Helm chart, per-user values overlay.',
        'semantic', 'demo,project', 0.9,
    ),
    (
        'project', 'stack',
        'Backend: Python 3 (stdlib http.server) at charts/workspace/server.py. '
        'Frontend: Vite + Preact + TypeScript in charts/workspace/web/. '
        'Storage: SQLite at /home/dev/.claude-memory/memory.db for memories, '
        'JSON files under /home/dev/.claude-tasks/ for task transcripts. '
        'Deploy: Helm chart at charts/workspace/, per-user overlay in '
        'users-private/<user>/values.yaml (gitignored).',
        'semantic', 'demo,project,stack', 0.9,
    ),
    (
        'project', 'auth-modes',
        'Three values.yaml ingress.auth.type modes: "basic" (htpasswd), '
        '"oauth2" (oauth2-proxy → GitHub), "none" (this public demo — '
        'hard-coupled to readOnly:true so server.py refuses to start in any '
        'other combo).',
        'semantic', 'demo,project,security', 0.85,
    ),
    (
        'feedback', 'test-before-claiming-fix',
        'After deploying a change, hit the real endpoint with curl AND open '
        'the dashboard in a browser before reporting the work as complete. '
        'Type-check + unit tests verify code correctness, not feature '
        'correctness — a UI bug can ship a green CI.',
        'procedural', 'demo,workflow', 0.7,
    ),
    (
        'feedback', 'use-makefile-targets',
        'Never invoke raw helm/buildx/kubectl for recurring flows. Every '
        'common operation has a Makefile target (make ship USER=<name>, '
        'make logs USER=<name>, make shell USER=<name>). If something is '
        'missing, add a target — do not call the tool directly.',
        'procedural', 'demo,workflow', 0.7,
    ),
    (
        'reference', 'docs-route',
        'In-app documentation lives at /docs in the dashboard SPA. Source '
        'pages are markdown files in the repo at charts/workspace/docs/ — '
        'edited there, served by /api/docs and /api/docs/<page> from '
        'server.py.',
        'semantic', 'demo,docs', 0.6,
    ),
    (
        'reference', 'memory-mcp',
        'The memory store is also exposed to Claude itself via an MCP server '
        '(charts/workspace/mcp_memory.py). Tasks spawn with KC_TASK_ID in '
        'env, so the MCP server can attribute every memory write back to '
        'the task that produced it. See the Memory tab — entries created '
        'by a Claude session carry a source="task:<id>" tag.',
        'semantic', 'demo,memory,mcp', 0.65,
    ),
    (
        'project', 'public-demo-limits',
        'On this public deploy: New Build / Edit / Delete / Kill / Rename / '
        'Send are hidden in the UI and the server rejects POST/DELETE with '
        '403. /terminal, /vnc-direct, /websockify, /browser ingresses are '
        'omitted from the helm chart entirely so visitors can never reach '
        'ttyd or the VNC desktop. Reads (list memories, browse docs, view '
        'this seeded task transcript) work normally.',
        'semantic', 'demo,security', 0.85,
    ),
]


def seed_memories() -> int:
    try:
        from memory.manager import MemoryManager
        from memory import store as memstore
    except ImportError as e:
        print(f'[seed_demo] memory package import failed: {e}', file=sys.stderr)
        return 0
    memstore.initialize()
    with MemoryManager.store().conn() as c:
        existing = c.execute('SELECT COUNT(*) AS n FROM memories WHERE deleted_at IS NULL').fetchone()
    if existing and existing['n'] > 0:
        print(f'[seed_demo] memories table has {existing["n"]} rows — skipping memory seed.')
        return 0
    written = 0
    for namespace, key, value, kind, tags, importance in DEMO_MEMORIES:
        try:
            MemoryManager.upsert(
                namespace=namespace, key=key, value=value, kind=kind,
                tags=tags, importance=importance, source='seed_demo',
            )
            written += 1
        except Exception as e:
            print(f'[seed_demo] upsert {namespace}.{key} failed: {e}', file=sys.stderr)
    print(f'[seed_demo] wrote {written} demo memories.')
    return written


# Each tuple: (prompt, output_log_body, status, assistant)
DEMO_TASKS = [
    (
        'Walk me through how kube-coder authenticates the dashboard. What\'s '
        'the difference between basic / oauth2 / none modes?',
        '''Looking at the helm chart for auth wiring.

charts/workspace/values.yaml:23-31
  ingress:
    auth:
      type: basic    # or "oauth2" or "none"

Three modes, each implemented by a different ingress template:

1. **basic** (default for personal workspaces):
   - templates/ingress.yaml adds nginx.ingress.kubernetes.io/auth-type: basic
   - Single htpasswd Secret named via .ingress.auth.secretName
   - Lightest-weight; one shared password
   - Good for solo / 1-2 person workspaces

2. **oauth2** (templates/ingress-oauth2.yaml + ingress-oauth2-api.yaml):
   - oauth2-proxy pod sits in front of the workspace ingress
   - Backed by a GitHub OAuth App; .oauth2.githubUsers is a comma-list
   - /oauth/* routes carry X-Auth-Request-* headers into the pod
   - Pairs with ingress-claude-api.yaml for bearer-token API access

3. **none** (templates/ingress-public.yaml — this demo!):
   - Bare TLS, no auth proxy
   - server.py refuses to start unless readOnly: true is also set
   - All mutation endpoints return 403; terminal/VNC ingresses skipped

The server.py side: check_claude_auth() short-circuits to True when
AUTH_MODE=none, and _readonly_block() gates do_POST / do_DELETE on
READONLY_MODE. Both env vars are wired in templates/deployment.yaml.

That\'s the full chain — let me know if you want to see the actual env
plumbing or the helm template diff.
''',
        'completed', 'claude',
    ),
    (
        'How does the memory subsystem rank which memories get auto-injected '
        'into a new task prompt?',
        '''Reading charts/workspace/memory/manager.py to walk through the
ranking logic.

The entrypoint is `MemoryManager.top_for_prompt(prompt)` (manager.py:521).
It returns up to K memories, scored by:

  score = recency_decay * importance * (1 + access_count_log)
        + bm25_relevance * (1 if vector_search disabled else 0.5)
        + cosine_similarity * (0 if vector_search disabled else 0.5)

Phase 1 (this deploy) is FTS5-only — no embeddings — so the cosine term
is zero and BM25 carries the relevance signal.

Then _rerank_score (manager.py:503) re-orders the top candidates by:
  - recency:   1.0 if updated in last 24h, decays exponentially to 0.1 at 30d
  - importance: clamped [0..1] as written
  - access_count: log1p(access_count) / log1p(50) — capped contribution

Anything below minScore (values.yaml: 0.30) is dropped. The remaining
list is wrapped in <workspace_memories> tags via format_injection_block
and prepended to the prompt before it\'s pasted into the tmux session.

Tunables in values.yaml:
  memory.inject.enabled  (true)
  memory.inject.topK     (8)
  memory.inject.minScore (0.30)
  memory.inject.maxChars (4096)

A failure in any of this never blocks task creation — server.py wraps
the call in a try/except and just emits an empty injection block.
''',
        'completed', 'claude',
    ),
    (
        'Add a new top-level route called /reports to the dashboard SPA.',
        '''Adding /reports as a new top-level SPA route.

Steps:

1. **Register the route** in charts/workspace/web/src/store/router.ts and
   the SPA_TOP_LEVEL set in charts/workspace/server.py:2212 so the server
   knows to serve index.html for /reports deep links.

2. **Create the route file** at
   charts/workspace/web/src/routes/reports/index.tsx — start with an
   empty <div class="route route-reports"><h1 class="route-title">
   Reports</h1></div> shell and import it from app.tsx.

3. **Add nav entries** in two places:
   - components/Rail.tsx — desktop left rail
   - components/BottomNav.tsx — mobile bottom bar
   Both use the same icon name from components/Icon.tsx; add the icon
   to that file if needed.

4. **Smoke-test locally**:
     make dashboard-web
     DASHBOARD_DIST_DIR=$PWD/charts/workspace/web/dist \\
       python3 charts/workspace/server.py
     # then open http://localhost:6080/reports

5. **Deploy**: make ship-config USER=<name> (configmap-only path — picks
   up the new SPA bundle + server.py route change without rebuilding the
   docker image).

Want me to wire the actual report-fetch API too, or keep the route
empty for now?
''',
        'completed', 'claude',
    ),
    (
        'Why does my pod sometimes take 90s to become ready after a redeploy?',
        '''Tracing the slow boot.

Likely culprits, in order of how often they bite:

1. **image pull on a fresh node** — DOKS sometimes schedules onto a node
   that hasn\'t pulled the workspace image before. The image is ~2GB
   (code-server + Firefox + Playwright + Claude CLI + OpenCode + tmux +
   ttyd + Xvfb + x11vnc + websockify + fluxbox). A cold pull on a 1Gbps
   node link takes ~25-30s before the container even starts.

2. **PVC remount on new node** — DigitalOcean Block Storage detaches
   from the old node and reattaches to the new one. Detach is usually
   <10s but attach can take 20-40s if the volume is busy.

3. **workspace-entrypoint serial stages** — start.sh runs several
   things synchronously (mkdir, OpenCode config render, MCP wiring,
   /tmp/browser copy). The git clone is backgrounded (see workspace-
   entrypoint-configmap.yaml:60-72 — the `&` at the end) so it doesn\'t
   block readiness, but everything before it is on the critical path.

4. **readinessProbe initialDelaySeconds: 30** (deployment.yaml:141) —
   kubelet waits 30s after container start before even probing
   /health on port 6080, so the *minimum* time from container-running
   to "Ready" is 30s + first-probe-pass.

If you want the boot faster:
  - Drop initialDelaySeconds to 5 — server.py answers /health within
    ~1s of process start
  - Add periodSeconds: 2 so kubelet retries quickly
  - The other items (image pull, PVC, entrypoint) are mostly fixed costs.
''',
        'completed', 'claude',
    ),
    (
        'Show me how the dashboard SPA fetches data from the workspace API.',
        '''Looking at the api client layer.

The whole HTTP layer is in charts/workspace/web/src/api/client.ts (132
lines, no framework). Key pieces:

**withOauthPrefix(path)** (client.ts:44)
  Prepends /oauth to every /api/* call. Production ingress only injects
  X-Auth-Request-* headers for paths under /oauth/*, so a bare /api/foo
  would arrive unauthenticated. server.py strips the prefix again
  (server.py:1931, 3381) so dev_server stays identical.

**api<T>(path, opts)** (client.ts:87)
  Generic fetch wrapper. Adds JSON Accept header, optional Bearer token
  from localStorage[\'kc.devToken\'], JSON-stringifies the body. On 401
  it bounces to /oauth2/start?rd=<current> so visitors see a real
  GitHub sign-in prompt instead of a silent CORS failure.

**apiGet / apiPost / apiDelete** (client.ts:129-131)
  Thin wrappers over api() that just pin the method.

Each feature has its own typed module that consumes apiGet/apiPost:
  api/memory.ts   — memory CRUD + history + neighbors
  api/tasks.ts    — task list, detail, output stream
  api/docs.ts     — docs manifest + page fetch
  api/triggers.ts — webhooks + crons
  api/system.ts   — /health, /metrics, /api/mode
  api/shape.ts    — coerce* helpers (cheap runtime schema coercion;
                    server response shape drifted once and we had a
                    bunch of `.length on undefined` crashes — see
                    coerceMemoryRecord and friends)

There\'s no react-query, no axios, no zod. Stdlib fetch + Preact signals
in src/store/* hold the cached server state.
''',
        'completed', 'claude',
    ),
]


def seed_tasks() -> int:
    os.makedirs(TASKS_DIR, mode=0o700, exist_ok=True)
    existing = [d for d in os.listdir(TASKS_DIR) if not d.startswith('.')]
    if existing:
        print(f'[seed_demo] tasks dir has {len(existing)} entries — skipping task seed.')
        return 0
    base_time = time.time() - 86400 * 5  # backdate so they look organic
    written = 0
    for i, (prompt, output, status, assistant) in enumerate(DEMO_TASKS):
        ts = base_time + i * 3600
        # task_id matches the format from server.py:570 — <epoch>-<hex>
        task_id = f'{int(ts)}-demo000{i}'
        task_dir = os.path.join(TASKS_DIR, task_id)
        os.makedirs(task_dir, mode=0o700, exist_ok=True)
        meta = {
            'task_id': task_id,
            'session_id': f'demo-session-{i:04d}',
            'prompt': prompt,
            'workdir': '/home/dev/kube-coder',
            'status': status,
            'created_at': ts,
            # Match server.py:list_tasks which reads 'finished_at' (formerly
            # written as 'completed_at' here, which the list view ignored —
            # demo tasks rendered with no finish time).
            'finished_at': ts + 90,
            'tmux_session': f'claude-{task_id}',
            'assistant': assistant,
            'memory_injected': [],
            'source': 'seed_demo',
        }
        with open(os.path.join(task_dir, 'task.json'), 'w') as f:
            json.dump(meta, f, indent=2)
        with open(os.path.join(task_dir, 'prompt.txt'), 'w') as f:
            f.write(prompt)
        with open(os.path.join(task_dir, 'output.log'), 'w') as f:
            f.write(f'> {prompt}\n\n{output}')
        written += 1
    print(f'[seed_demo] wrote {written} demo task transcripts.')
    return written


def main() -> int:
    if os.environ.get('READONLY_MODE', '').lower() != 'true':
        # Defensive: this script is wired into the entrypoint behind a
        # bash gate already, but if a curious operator runs it on a real
        # workspace we silently no-op rather than risk pollution.
        print('[seed_demo] READONLY_MODE not set — refusing to seed.')
        return 0
    m = seed_memories()
    t = seed_tasks()
    print(f'[seed_demo] done. memories={m} tasks={t}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
