#!/usr/bin/env node
/**
 * The README's product screenshots, regenerated from one coherent mock
 * workspace so every image tells the same story (same projects, same agents,
 * same numbers) and none of the operator's real data leaks in.
 *
 * Every /api, /metrics and /health call is answered here — a catch-all returns
 * an empty object, and the surfaces in the README get real-looking fixtures on
 * top. The dev server only has to serve the built SPA (dist/).
 *
 *   yarn --cwd charts/workspace/web build
 *   python3 charts/workspace/web/dev_server.py 7070 &
 *   node charts/workspace/web/scripts/shoot-readme.mjs docs/screenshots/readme
 *
 * 1440×900 CSS px at deviceScaleFactor 2 (2880×1800 PNGs).
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || 'docs/screenshots/readme');
mkdirSync(out, { recursive: true });
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
const ONLY = process.env.ONLY ? process.env.ONLY.split(',') : null;

const now = 1_763_740_000; // fixed epoch — the script must be deterministic

// ── Shared fixtures ────────────────────────────────────────────────────────
const METRICS = {
  cpu: { usage_percent: 23, cores: 4 },
  memory: { total_mb: 16384, used_mb: 6230, available_mb: 10154, percent: 38 },
  disk: { total_gb: 100, used_gb: 41, available_gb: 59, percent: 41, path: '/home/dev' },
  uploads: { used_bytes: 180e6, quota_bytes: 2e9, available_bytes: 1.82e9, percent: 9 },
  alerts: [],
  timestamp: now,
};
const HEALTH = {
  // Two readers: the topbar dot wants the booleans, the metrics view the map.
  vscode: true, terminal: true, browser: true,
  status: 'healthy',
  services: {
    vscode: { status: 'up', port: 8080 }, terminal: { status: 'up', port: 7681 },
    dashboard: { status: 'up', port: 6080 }, browser: { status: 'up', port: 6081 },
  },
  timestamp: now,
};

const PROJECTS = [
  {
    id: 'storefront', name: 'storefront', workdirs: ['/home/dev/storefront'],
    repo: 'acme/storefront', memory_namespace: 'project.storefront',
    status: 'active', north_star: 'Checkout conversion above 4% by launch',
    last_seen_at: now - 900, created_at: now - 900000, updated_at: now - 120,
    pulse: { running: 2, waiting: 1, last_activity_at: now - 120 },
  },
  {
    id: 'api', name: 'api', workdirs: ['/home/dev/api'],
    repo: 'acme/api', memory_namespace: 'project.api',
    status: 'active', north_star: 'p95 under 120ms on every public route',
    last_seen_at: now - 40000, created_at: now - 800000, updated_at: now - 30000,
    pulse: { running: 0, waiting: 0, last_activity_at: now - 30000 },
  },
];

const CARDS = [
  {
    id: 'build:t_pay', ref_id: 't_pay', kind: 'build', state: 'waiting',
    title: 'Stripe webhook retries',
    headline: 'Migration drops the old idempotency table — needs your OK',
    assistant: 'claude', model: '', workdir: '/home/dev/api', repo: 'api', branch: 'kc/webhook-retries',
    created_at: now - 3200, updated_at: now - 540, finished_at: null, waiting_since: now - 540,
    waiting_prompt: {
      kind: 'choice',
      question: 'Run `alembic upgrade head` (drops idempotency_keys_v1, 0 live rows)?',
      options: [{ index: 1, label: 'Yes' }, { index: 2, label: "Yes, and don't ask again" }, { index: 3, label: 'No, tell Claude what to do instead' }],
    },
    outcome: null, evidence: [], parent_id: null, children: [],
  },
  {
    id: 'build:t_checkout', ref_id: 't_checkout', kind: 'build', state: 'running',
    title: 'One-page checkout',
    headline: 'Editing src/checkout/Summary.tsx — wiring the promo-code field',
    assistant: 'claude', model: '', workdir: '/home/dev/storefront', repo: 'storefront', branch: 'kc/one-page-checkout',
    created_at: now - 2400, updated_at: now - 8, finished_at: null, waiting_since: null, waiting_prompt: null,
    outcome: null, evidence: [], parent_id: null,
    children: [{ id: 'subagent:t_e2e', title: 'e2e-tests', state: 'running' }],
  },
  {
    id: 'subagent:t_e2e', ref_id: 't_e2e', kind: 'subagent', state: 'running',
    title: 'e2e-tests',
    headline: 'Playwright: guest checkout, saved card, promo code — 9/11 green',
    assistant: 'codex', model: '', workdir: '/home/dev/storefront', repo: 'storefront', branch: 'kc/one-page-checkout',
    created_at: now - 700, updated_at: now - 20, finished_at: null, waiting_since: null, waiting_prompt: null,
    outcome: null, evidence: [], parent_id: 'build:t_checkout', children: [],
  },
  {
    id: 'chat:h_perf', ref_id: 'h_perf', kind: 'chat', state: 'running',
    title: 'Why is /search slow?',
    headline: 'Profiling the search handler — N+1 on product variants',
    assistant: 'claude', model: 'opus', workdir: '/home/dev/api', repo: 'api', branch: '',
    created_at: now - 5400, updated_at: now - 60, finished_at: null, waiting_since: null, waiting_prompt: null,
    outcome: null, evidence: [], parent_id: null, children: [],
  },
  {
    id: 'build:t_dark', ref_id: 't_dark', kind: 'build', state: 'done',
    title: 'Dark mode for the storefront',
    headline: 'Theme tokens + toggle; screenshots attached to the PR',
    assistant: 'claude', model: '', workdir: '/home/dev/storefront', repo: 'storefront', branch: 'kc/dark-mode',
    created_at: now - 12000, updated_at: now - 2600, finished_at: now - 2600, waiting_since: null, waiting_prompt: null,
    outcome: { ok: true, detail: 'completed' },
    evidence: [
      { label: 'vitest 312', ok: true, link: null },
      { label: 'lint clean', ok: true, link: null },
      { label: 'PR #184', ok: null, link: 'https://github.com/acme/storefront/pull/184' },
    ],
    parent_id: null, children: [],
  },
  {
    id: 'build:t_deps', ref_id: 't_deps', kind: 'build', state: 'done',
    title: 'Upgrade to React 19',
    headline: 'Two type errors left in legacy cart components',
    assistant: 'gemini', model: '', workdir: '/home/dev/storefront', repo: 'storefront', branch: 'kc/react-19',
    created_at: now - 20000, updated_at: now - 15000, finished_at: now - 15000, waiting_since: null, waiting_prompt: null,
    outcome: { ok: false, detail: 'error · exit 2' },
    evidence: [{ label: 'tsc 2 errors', ok: false, link: null }],
    parent_id: null, children: [],
  },
  {
    id: 'chat:h_docs', ref_id: 'h_docs', kind: 'chat', state: 'done',
    title: 'Write the API rate-limit docs',
    headline: 'docs/rate-limits.md drafted with examples per plan',
    assistant: 'claude', model: '', workdir: '/home/dev/api', repo: 'api', branch: '',
    created_at: now - 30000, updated_at: now - 26000, finished_at: null, waiting_since: null, waiting_prompt: null,
    outcome: { ok: true, detail: 'idle — resumable' }, evidence: [], parent_id: null, children: [],
  },
];
const QUEUE = { cards: CARDS, pulse: { running: 3, waiting: 1, done_today: 6, oldest_wait_s: 540, generated_at: now } };

const TASKS = [
  { task_id: 't_pay', name: 'Stripe webhook retries', prompt: 'Make Stripe webhook handling idempotent with retries', status: 'waiting-for-input', waiting_reason: 'permission', created_at: now - 3200, finished_at: null, source: 'dashboard', kind: 'claude', last_activity_at: now - 540, prompt_delivered: true, worktree: { branch: 'kc/webhook-retries', port: null, path: '/home/dev/.worktrees/api/webhook-retries', removed: false, stat: null } },
  { task_id: 't_checkout', name: 'One-page checkout', prompt: 'Collapse checkout into a single page with inline validation', status: 'running', created_at: now - 2400, finished_at: null, source: 'dashboard', kind: 'claude', last_activity_at: now - 8, prompt_delivered: true, worktree: { branch: 'kc/one-page-checkout', port: 5173, path: '/home/dev/.worktrees/storefront/one-page-checkout', removed: false, stat: null } },
  { task_id: 't_e2e', name: 'e2e-tests', prompt: 'Write Playwright coverage for the new checkout', status: 'running', parent_task_id: 't_checkout', created_at: now - 700, finished_at: null, source: 'agent', kind: 'claude', last_activity_at: now - 20, prompt_delivered: true },
  { task_id: 't_dark', name: 'Dark mode for the storefront', prompt: 'Add a dark theme with a toggle', status: 'completed', created_at: now - 12000, finished_at: now - 2600, source: 'dashboard', kind: 'claude', last_activity_at: now - 2600, prompt_delivered: true },
  { task_id: 't_deps', name: 'Upgrade to React 19', prompt: 'Upgrade the storefront to React 19', status: 'error', created_at: now - 20000, finished_at: now - 15000, source: 'cron', kind: 'claude', last_activity_at: now - 15000, prompt_delivered: true },
];
const OUTPUT = {
  t_pay: '● Read(migrations/versions/0042_idempotency.py)\n● Bash(alembic upgrade head --sql | head -20)\n  DROP TABLE idempotency_keys_v1;\n\nDo you want to run alembic upgrade head?\n❯ 1. Yes\n  2. Yes, and don\'t ask again\n  3. No',
  t_checkout: '● Update(src/checkout/Summary.tsx)\n  ⎿ Added 34 lines, removed 12 lines\n● Bash(npm run test -- checkout)\n  ⎿ ✓ 41 passed (2.1s)\n✻ Wiring the promo-code field…',
  t_e2e: '● Write(e2e/checkout.spec.ts)\n● Bash(npx playwright test e2e/checkout.spec.ts)\n  ⎿ 9 passed, 2 running\n✻ Waiting on saved-card flow…',
};

const CONFIG = {
  enabled: true,
  assistants: [
    { id: 'claude', label: 'Claude Code', model: 'opus', models: ['opus', 'sonnet'], efforts: ['low', 'medium', 'high', 'xhigh', 'max'], effort: 'high', default: true },
    { id: 'codex', label: 'Codex', model: 'gpt-5', models: ['gpt-5'] },
    { id: 'gemini', label: 'Gemini' },
  ],
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
};

const THREADS = [
  { id: 'c_live', title: 'Show me the checkout running', assistant: 'claude', model: 'opus', status: 'idle', persona: '', project_id: 'storefront', workdir: '/home/dev/storefront', created_at: now - 1800, updated_at: now - 40 },
  { id: 'c_ops', title: "What's running and how much CPU?", assistant: 'claude', model: 'opus', status: 'idle', persona: '', project_id: '', workdir: '/home/dev', created_at: now - 3600, updated_at: now - 600 },
  { id: 'c_cto', title: 'What should we ship before launch?', assistant: 'claude', model: 'opus', status: 'idle', persona: 'cto', project_id: 'storefront', workdir: '/home/dev/storefront', created_at: now - 7000, updated_at: now - 300 },
  { id: 'c_perf', title: 'Why is /search slow?', assistant: 'claude', model: 'opus', status: 'running', persona: '', project_id: 'api', workdir: '/home/dev/api', created_at: now - 5400, updated_at: now - 60 },
  { id: 'c_board', title: 'Triage ACME-212', assistant: 'claude', status: 'idle', persona: 'board', project_id: 'api', workdir: '/home/dev/api', created_at: now - 40000, updated_at: now - 20000 },
];

let seq = 0;
const ev = (role, type, extra) => ({ seq: ++seq, ts: now - 400 + seq * 10, role, type, ...extra });
const EVENTS = {
  c_live: [
    ev('user', 'message', { text: 'Start the storefront dev server and show me the new checkout' }),
    ev('assistant', 'message', { text: "I'll start the dev server in the checkout worktree and pin it to Apps." }),
    ev('assistant', 'tool_call', { tool: { name: 'mcp__orchestrator__run_shell', input: { command: 'npm run dev -- --port 5173', workdir: '/home/dev/.worktrees/storefront/one-page-checkout' } }, tool_id: 'a1' }),
    ev('assistant', 'tool_result', { tool_use_id: 'a1', text: 'VITE v6.3 ready in 412 ms  ➜  Local: http://localhost:5173/' }),
    ev('assistant', 'tool_call', { tool: { name: 'mcp__dashboard__show_app_preview', input: { port: 5173, title: 'Storefront — one-page checkout', height: 470 } }, tool_id: 'a2' }),
    ev('assistant', 'message', { text: "It's live on **:5173**. Shipping, payment and the promo field are now one page; validation runs inline as you type. The e2e sub-agent has 9 of 11 flows green — I'll ping you when the saved-card flow passes." }),
  ],
  c_ops: [
    ev('user', 'message', { text: "What's running and how much CPU am I using?" }),
    ev('assistant', 'tool_call', { tool: { name: 'mcp__dashboard__get_metrics', input: {} }, tool_id: 'b1' }),
    ev('assistant', 'tool_result', { tool_use_id: 'b1', text: 'cpu 23% of 4 cores · memory 6.1 / 16 GiB · disk 41%' }),
    ev('assistant', 'tool_call', { tool: { name: 'mcp__orchestrator__list_tasks', input: { status: 'active' } }, tool_id: 'b2' }),
    ev('assistant', 'tool_result', { tool_use_id: 'b2', text: '3 active: t_checkout (running), t_e2e (running), t_pay (waiting)' }),
    ev('assistant', 'message', { text: "You're at **23% CPU** across 4 cores and **38% memory** — plenty of headroom.\n\nThree agents are active:\n\n| Build | State | Doing |\n|---|---|---|\n| One-page checkout | running | wiring the promo-code field |\n| e2e-tests | running | 9/11 Playwright flows green |\n| Stripe webhook retries | **waiting on you** | wants to run a migration |\n\nWant me to approve the migration, or open it so you can read the SQL first?" }),
  ],
  c_cto: [
    ev('user', 'message', { text: 'What should we ship before launch?' }),
    ev('assistant', 'message', { text: "Three things compete for the week before launch, in this order:\n\n1. **One-page checkout** — it moves the north-star directly (checkout conversion) and is already 80% done.\n2. **Webhook retries** — a dropped Stripe webhook today means an unpaid order; that's a correctness bug, not polish.\n3. **React 19** — park it. It failed on two legacy cart components and buys nothing users can see.\n\nI'd keep two builds running and hold the third." }),
  ],
};

const BRIEF = {
  project: PROJECTS[0],
  tasks: {
    running: 2, waiting: 1, total: 9,
    recent: [
      { task_id: 't_checkout', status: 'running', prompt: 'One-page checkout', workdir: '/home/dev/storefront', assistant: 'claude', last_activity_at: now - 8 },
      { task_id: 't_e2e', status: 'running', prompt: 'e2e-tests for checkout', workdir: '/home/dev/storefront', assistant: 'codex', last_activity_at: now - 20 },
      { task_id: 't_pay', status: 'waiting-for-input', prompt: 'Stripe webhook retries', workdir: '/home/dev/api', assistant: 'claude', last_activity_at: now - 540 },
    ],
  },
  goals: [{ namespace: 'project.storefront.goals', key: 'conversion', value: 'Checkout conversion above 4% by launch', tags: ['goal'], importance: 0.9, updated_at: now - 8000 }],
  decisions: [
    { namespace: 'project.storefront.decisions', key: 'one-page', value: 'Single-page checkout; no account required to pay', tags: ['decision'], importance: 0.8, updated_at: now - 4000 },
    { namespace: 'project.storefront.decisions', key: 'react19', value: 'React 19 waits until after launch', tags: ['decision'], importance: 0.7, updated_at: now - 300 },
  ],
  memories: [{ namespace: 'project.storefront', key: 'deploy', value: 'Deploy with `make ship`; preview envs per PR', tags: [], importance: 0.5, updated_at: now - 500000 }],
  git: [{ workdir: '/home/dev/storefront', branch: 'kc/one-page-checkout', exists: true }],
  counts: { goals: 1, decisions: 2, memories: 1, tasks: 9 },
  brief_markdown: '# storefront — project brief',
};

const DESKTOP = {
  items: [
    { id: 'd1', label: 'Run tests', icon: '🧪', action: { type: 'task', prompt: 'Run the full test suite and fix anything red', workdir: '/home/dev/storefront' } },
    { id: 'd2', label: 'Deploy preview', icon: '🚀', action: { type: 'shell', command: 'make preview' } },
    { id: 'd3', label: 'Storefront', icon: '🛍️', action: { type: 'url', url: 'http://localhost:5173', target: 'blank' } },
    { id: 'd4', label: 'Grafana', icon: '📈', action: { type: 'url', url: 'https://grafana.example.com', target: 'blank' } },
  ],
};

// The app the Chat embeds: a small, believable storefront checkout page.
const STOREFRONT = `<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,'Inter',sans-serif;background:#fafaf9;color:#1c1917}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 28px;background:#fff;border-bottom:1px solid #e7e5e4}
.logo{font-weight:800;letter-spacing:-.02em;font-size:17px}.logo span{color:#ea580c}
nav{display:flex;gap:18px;color:#57534e;font-size:13px}
main{display:grid;grid-template-columns:1.4fr 1fr;gap:22px;padding:24px 28px;max-width:980px;margin:0 auto}
.card{background:#fff;border:1px solid #e7e5e4;border-radius:12px;padding:18px}
h2{font-size:13px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.06em;color:#78716c}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
label{display:block;font-size:12px;color:#57534e;margin-bottom:4px}
.in{border:1px solid #d6d3d1;border-radius:8px;padding:9px 11px;background:#fff;font-size:13px}
.ok{border-color:#16a34a;box-shadow:0 0 0 3px #16a34a22}
.item{display:flex;gap:12px;align-items:center;padding:8px 0;border-bottom:1px solid #f5f5f4}
.thumb{width:44px;height:44px;border-radius:8px;background:linear-gradient(135deg,#fed7aa,#fb923c)}
.thumb.b{background:linear-gradient(135deg,#bae6fd,#38bdf8)}
.grow{flex:1}.muted{color:#78716c;font-size:12px}
.tot{display:flex;justify-content:space-between;padding:6px 0}.tot.big{font-weight:700;font-size:16px;border-top:1px solid #e7e5e4;margin-top:6px;padding-top:10px}
.promo{display:flex;gap:8px;margin:10px 0}.promo .in{flex:1}
.applied{color:#16a34a;font-size:12px;font-weight:600}
button{width:100%;margin-top:12px;padding:12px;border:0;border-radius:10px;background:#ea580c;color:#fff;font-weight:700;font-size:14px}
</style></head><body>
<header><div class="logo">acme<span>.</span>shop</div><nav><span>Shop</span><span>Collections</span><span>Cart (2)</span></nav></header>
<main><div class="card"><h2>Shipping</h2>
<div class="row"><div><label>First name</label><div class="in ok">Dana</div></div><div><label>Last name</label><div class="in ok">Rivera</div></div></div>
<div style="margin-bottom:10px"><label>Address</label><div class="in ok">221 Mission St, San Francisco</div></div>
<h2 style="margin-top:16px">Payment</h2>
<div class="row"><div><label>Card</label><div class="in ok">•••• •••• •••• 4242</div></div><div><label>Expiry · CVC</label><div class="in ok">08/28 · •••</div></div></div>
</div><div class="card"><h2>Order</h2>
<div class="item"><div class="thumb"></div><div class="grow">Trail Runner 2<div class="muted">Size 10 · Ember</div></div><div>$128</div></div>
<div class="item"><div class="thumb b"></div><div class="grow">Merino Crew<div class="muted">M · Glacier</div></div><div>$64</div></div>
<div class="promo"><div class="in">LAUNCH15</div></div><div class="applied">✓ LAUNCH15 applied — 15% off</div>
<div class="tot"><span class="muted">Subtotal</span><span>$192.00</span></div>
<div class="tot"><span class="muted">Discount</span><span>−$28.80</span></div>
<div class="tot big"><span>Total</span><span>$163.20</span></div>
<button>Pay $163.20</button></div></main></body></html>`;

// What the Build's terminal iframe (ttyd) shows: a Claude Code session mid-task.
const TERMINAL = `<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;background:#0d1117;color:#c9d1d9;font:13px/1.55 Menlo,'SF Mono',monospace;padding:14px 18px;white-space:pre}
.d{color:#6e7681}.g{color:#3fb950}.y{color:#d29922}.b{color:#58a6ff}.m{color:#bc8cff}.o{color:#f0883e}.w{color:#e6edf3;font-weight:600}
.box{border:1px solid #30363d;border-radius:6px;padding:8px 12px;display:inline-block;margin:6px 0}
</style></head><body><span class="o">✻</span> <span class="w">Welcome to Claude Code</span>  <span class="d">~/.worktrees/storefront/one-page-checkout</span>

<span class="d">&gt;</span> Collapse checkout into a single page with inline validation

<span class="g">●</span> <span class="w">Read</span>(src/checkout/Shipping.tsx, src/checkout/Payment.tsx)
  <span class="d">⎿  Read 212 lines</span>

<span class="g">●</span> I'll merge the three steps into one <span class="b">CheckoutPage</span> and validate each field on blur.

<span class="g">●</span> <span class="w">Write</span>(src/checkout/CheckoutPage.tsx)
  <span class="d">⎿  Wrote 168 lines</span>

<span class="g">●</span> <span class="w">Update</span>(src/checkout/Summary.tsx)
  <span class="d">⎿  Added 34 lines, removed 12 lines</span>
     <span class="g">+  const [promo, setPromo] = useState&lt;Promo | null&gt;(null);</span>
     <span class="g">+  const total = applyPromo(subtotal, promo);</span>
     <span class="y">-  return &lt;StepSummary step={3} /&gt;;</span>

<span class="g">●</span> <span class="w">Bash</span>(npm run test -- checkout)
  <span class="d">⎿</span>  <span class="g">✓</span> src/checkout/CheckoutPage.test.tsx <span class="d">(18)</span>
     <span class="g">✓</span> src/checkout/Summary.test.tsx <span class="d">(23)</span>
     <span class="g">Test Files  2 passed · Tests  41 passed</span> <span class="d">(2.1s)</span>

<span class="g">●</span> <span class="w">Task</span>(e2e-tests) <span class="d">— spawned sub-agent: Playwright coverage for guest, saved-card and promo flows</span>

<span class="m">✻ Wiring the promo-code field…</span> <span class="d">(42s · ↓ 3.1k tokens · esc to interrupt)</span>

<span class="box"><span class="d">&gt;</span> <span class="d">Try "add Apple Pay to the payment step"</span></span>
<span class="d">  ⏵⏵ auto-accept edits on · 1 sub-agent running</span></body></html>`;

// ── Mocking ────────────────────────────────────────────────────────────────
async function mockAll(page) {
  const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  // Catch-all FIRST: Playwright matches the most recently registered route,
  // so every specific mock below wins over this.
  await page.route(/\/api\//, (r) => json(r, {}));
  await page.route(/\/(oauth\/)?metrics(\?|$)/, (r) => json(r, METRICS));
  await page.route(/\/(oauth\/)?health(\?|$)/, (r) => json(r, HEALTH));
  await page.route(/\/api\/events/, (r) => r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }));
  await page.route(/\/api\/mode/, (r) => json(r, { readOnly: false, authed: true, authMode: 'oauth2', ctoEnabled: true }));
  await page.route(/\/api\/feed\/unread_count/, (r) => json(r, { count: 3 }));
  await page.route(/\/api\/subscriptions/, (r) => json(r, { subscriptions: {}, claude_ready: true }));
  await page.route(/\/api\/missioncontrol\/queue/, (r) => json(r, QUEUE));
  await page.route(/\/api\/desktop(\?|$)/, (r) => json(r, DESKTOP));
  await page.route(/\/api\/apps(\?|$)/, (r) => json(r, { apps: [{ port: 5173, name: 'storefront', pinned: true }] }));
  await page.route(/\/api\/workspace\/dirs/, (r) => json(r, { dirs: PROJECTS.map((p) => ({ path: p.workdirs[0], label: p.name, is_git: true })) }));
  await page.route(/\/api\/projects(\?|$)/, (r) => json(r, { projects: PROJECTS }));
  await page.route(/\/api\/projects\/_discover/, (r) => json(r, { candidates: [], registered: [] }));
  await page.route(/\/api\/projects\/[^/?]+(\?|$)/, (r) => json(r, PROJECTS[0]));
  await page.route(/\/api\/projects\/storefront\/brief/, (r) => json(r, BRIEF));
  await page.route(/\/api\/hypervisor\/config/, (r) => json(r, CONFIG));
  await page.route(/\/api\/hypervisor\/threads(\?|$)/, (r) => json(r, { threads: THREADS }));
  await page.route(/\/api\/hypervisor\/threads\/[^/?]+/, (r) => {
    const id = new URL(r.request().url()).pathname.split('/')[4];
    const thread = THREADS.find((t) => t.id === id) || THREADS[0];
    return json(r, { thread, events: EVENTS[thread.id] || [], source: 'session_log' });
  });
  await page.route(/\/api\/claude\/tasks(\?|$)/, (r) => json(r, { tasks: TASKS }));
  await page.route(/\/api\/hypervisor\/threads\/[^/?]+\/board-requests/, (r) => json(r, { requests: [] }));
  await page.route(/\/api\/claude\/tasks\/t_[a-z0-9]+(\?|$)/, (r) => {
    const id = new URL(r.request().url()).pathname.split('/')[4];
    const t = TASKS.find((x) => x.task_id === id) || TASKS[1];
    return json(r, { ...t, workdir: '/home/dev/storefront', assistant: 'claude', recent_output: OUTPUT[id] || '', pending_prompt: null });
  });
  await page.route(/\/terminal\//, (r) => r.fulfill({ contentType: 'text/html', body: TERMINAL }));
  await page.route(/\/api\/claude\/tasks\/[^/]+\/output/, (r) => {
    const id = new URL(r.request().url()).pathname.split('/')[4];
    return json(r, { output: OUTPUT[id] || '' });
  });
  await page.route(/\/api\/app-proxy\/5173/, (r) => r.fulfill({ contentType: 'text/html', body: STOREFRONT }));
  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
  await page.addInitScript(() => {
    localStorage.setItem('kc.onboardingDone', 'true');
    localStorage.setItem('kc.hvBriefCollapsed', '0');
    localStorage.setItem('kc.guide.hypervisor', 'collapsed');
    localStorage.setItem('kc.term.copyHintDismissed', '1');
  });
}

// ── Shots ──────────────────────────────────────────────────────────────────
const VP = { width: 1440, height: 900 };
const SHOTS = [
  { name: 'chat-app-preview', path: '/hypervisor/c_live', theme: 'dark', wait: '.hv-embed' },
  { name: 'chat-tools', path: '/hypervisor/c_ops', theme: 'dark', wait: '.route-hypervisor' },
  { name: 'chat-cto-brief', path: '/hypervisor/c_cto', theme: 'dark', wait: '.cto-brief' },
  { name: 'mission-control', path: '/mission', theme: 'dark', wait: '.route-mission' },
  { name: 'desktop-home', path: '/desktop', theme: 'dark', wait: 'main' },
  { name: 'desktop-home-light', path: '/desktop', theme: 'light', wait: 'main' },
  { name: 'builds', path: '/tasks/t_checkout', theme: 'dark', wait: '.tl-row-preview' },
  { name: 'mission-control-mobile', path: '/mission', theme: 'dark', wait: '.route-mission', viewport: { width: 390, height: 844 } },
  { name: 'chat-mobile', path: '/hypervisor/c_ops', theme: 'dark', wait: '.route-hypervisor', viewport: { width: 390, height: 844 } },
];

const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });
try {
  for (const s of SHOTS) {
    if (ONLY && !ONLY.includes(s.name)) continue;
    const ctx = await browser.newContext({ viewport: s.viewport || VP, deviceScaleFactor: 2, colorScheme: s.theme });
    const page = await ctx.newPage();
    await mockAll(page);
    await page.goto(`${BASE}${s.path}`, { waitUntil: 'load' });
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), s.theme);
    await page.waitForSelector(s.wait, { timeout: 15000 }).catch(() => console.warn(`  (no ${s.wait} on ${s.name})`));
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${out}/${s.name}.png` });
    await ctx.close();
    console.log(`${s.name}.png`);
  }
} finally {
  await browser.close();
}
