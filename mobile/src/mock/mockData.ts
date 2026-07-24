/**
 * Mock backend. Used by the demo/screenshot build (EXPO_PUBLIC_MOCK=1) so the
 * app renders a populated, working UI without a live workspace. The client
 * (src/api/client.ts) routes here when config.mock is true.
 *
 * Timestamps are offsets from load time so the relative labels ("90s ago",
 * "1h ago") read realistically whenever screenshots are regenerated.
 */
import type {
  AppEntry,
  ControllerCapacity,
  ControllerWorkspace,
  DesktopItem,
  FileEntry,
  FileListing,
  FilePreview,
  Health,
  MemoryRecord,
  Metrics,
  MissionCard,
  MissionQueue,
  PreviewControlAction,
  PreviewControlResult,
  PreviewMessage,
  PreviewSendResult,
  PreviewState,
  SkillRecord,
  TaskDetail,
  TaskSummary,
  WorkdirOption,
} from '../api/types';

const NOW = Math.floor(Date.now() / 1000);

export const mockWorkdirs: WorkdirOption[] = [
  { path: '/home/dev/kube-coder', label: 'kube-coder', is_git_repo: true },
  { path: '/home/dev/storefront', label: 'storefront', is_git_repo: true },
  { path: '/home/dev/api-service', label: 'api-service' },
];

export const mockTasks: TaskSummary[] = [
  {
    id: 'a1b2c3',
    prompt: 'Add a /healthz endpoint to server.py and a unit test for it',
    status: 'running',
    assistant: 'claude',
    workdir: '/home/dev/kube-coder',
    created_at: NOW - 90,
    updated_at: NOW - 5,
  },
  {
    id: 'd4e5f6',
    prompt: 'Refactor the auth middleware to share the Bearer-token check',
    status: 'waiting',
    assistant: 'claude',
    workdir: '/home/dev/kube-coder',
    created_at: NOW - 600,
    updated_at: NOW - 40,
    waiting_for_input: true,
  },
  {
    id: 'p7q8r9',
    prompt: 'Polish the onboarding animation and add haptics to the connect button',
    status: 'running',
    assistant: 'ante',
    workdir: '/home/dev/storefront',
    created_at: NOW - 240,
    updated_at: NOW - 12,
  },
  {
    id: 'g7h8i9',
    prompt: 'Write integration tests for the webhook receiver',
    status: 'done',
    assistant: 'claude',
    workdir: '/home/dev/api-service',
    created_at: NOW - 3600,
    updated_at: NOW - 3000,
  },
  {
    id: 'j1k2l3',
    prompt: 'Bump the Helm chart version and regenerate the README table',
    status: 'done',
    assistant: 'ante',
    workdir: '/home/dev/kube-coder',
    created_at: NOW - 7200,
    updated_at: NOW - 6800,
  },
  {
    id: 'm4n5o6',
    prompt: 'Investigate the flaky terminal scroll test on CI',
    status: 'error',
    assistant: 'claude',
    workdir: '/home/dev/kube-coder',
    created_at: NOW - 10800,
    updated_at: NOW - 10500,
  },
];

const mockOutputs: Record<string, string> = {
  a1b2c3: `● I'll add a /healthz endpoint and a test.

  Read server.py (3694-3710)
  Edit server.py
    + def do_healthz(self):
    +     self.send_response(200)
    +     self.send_json({"status": "ok"})

● Now adding the unit test...

  Write tests/healthz_test.py
    + def test_healthz_returns_ok(self):
    +     resp = self.client.get('/healthz')
    +     self.assertEqual(resp.status_code, 200)

● Running the test suite

  $ python3 -m unittest tests/healthz_test.py
  ....
  Ran 4 tests in 0.12s
  OK

  Working on the final wiring…`,
  d4e5f6: `● I found three call sites that re-implement the Bearer check:
    - server.py:3694  check_claude_auth()
    - server.py:4821  verify_token()
    - server.py:5102  _bearer_ok()

  I can extract a single require_bearer() helper. This changes the
  401 response body shape slightly (adds an "code" field).

❓ Proceed with the shared helper, or keep responses byte-identical?
   [waiting for your reply]`,
  g7h8i9: `● Integration tests for the webhook receiver are complete.

  Write tests/webhook_receiver_test.py  (+142 lines)

  $ pytest tests/webhook_receiver_test.py
  ........................
  24 passed in 1.8s

✓ Done. Covered: HMAC validation, replay rejection, oversized body,
  unknown provider, and the happy path for github/stripe/slack.`,
};

export function mockTaskDetail(id: string): TaskDetail | null {
  const t = mockTasks.find((x) => x.id === id);
  if (!t) return null;
  return {
    ...t,
    output: mockOutputs[id] ?? `● Task ${id}\n\n  (no recent output captured)`,
    tmux_session: `claude-${id}`,
  };
}

export const mockMemory: MemoryRecord[] = [
  {
    namespace: 'user.preferences',
    key: 'editor',
    value: 'VS Code with Vim keybindings',
    tags: ['editor'],
    importance: 0.7,
    updated_at: NOW - 86400,
  },
  {
    namespace: 'user.preferences',
    key: 'language',
    value: 'Go for services, TypeScript for UI',
    tags: ['lang'],
    importance: 0.8,
    updated_at: NOW - 172800,
  },
  {
    namespace: 'project.kube-coder',
    key: 'deploy_cmd',
    value: 'make ship USER=<name> — builds the image tag from values.yaml',
    tags: ['deploy', 'ops'],
    importance: 0.9,
    updated_at: NOW - 43200,
  },
  {
    namespace: 'project.kube-coder',
    key: 'mobile_build',
    value: 'eas build --profile production; upload .ipa via eas submit',
    tags: ['mobile', 'ci'],
    importance: 0.85,
    updated_at: NOW - 3600,
  },
];

export const mockSkills: SkillRecord[] = [
  {
    name: 'remote-task',
    description: 'Launch a Claude task on a remote kube-coder workspace, check status, or attach.',
    body: '# Remote Task Skill\n\nLaunch tasks on remote workspace pods.',
    scope: 'project',
    systems: ['claude', 'opencode', 'ante'],
    user_invocable: true,
    allowed_tools: ['Bash', 'Read', 'Grep'],
    argument_hint: '[prompt or "status"]',
    updated_at: NOW - 7200,
  },
  {
    name: 'code-review',
    description: 'Review the current diff for correctness bugs and cleanups.',
    body: '# Code Review\n\nRuns a structured review over the working diff.',
    scope: 'user',
    systems: ['claude'],
    user_invocable: true,
    allowed_tools: ['Bash', 'Read'],
    updated_at: NOW - 86400,
  },
  {
    name: 'deploy-prod',
    description: 'Guarded production deploy runbook.',
    body: 'Run make ship, verify rollout, watch alerts for 10 minutes.',
    scope: 'user',
    systems: ['opencode'],
    user_invocable: false,
    updated_at: NOW - 43200,
  },
];

export const mockMetrics: Metrics = {
  cpu_percent: 37,
  memory_used_mb: 2148,
  memory_total_mb: 4096,
  disk_used_gb: 12.4,
  disk_total_gb: 50,
};

export const mockHealth: Health = {
  vscode: true,
  terminal: true,
  browser: false,
  ok: true,
};

export const mockDesktop: DesktopItem[] = [
  {
    id: 'seed-build',
    label: 'Fix flaky test',
    icon: 'icon:chat',
    action: { type: 'task', prompt: 'Find and fix the flaky integration test', workdir: '/home/dev/kube-coder' },
  },
  {
    id: 'seed-store',
    label: 'Storefront',
    icon: '🛍️',
    action: { type: 'url', url: 'https://demo-public.dev.scalebase.io', target: 'blank' },
  },
  {
    id: 'seed-tests',
    label: 'Run tests',
    icon: 'icon:terminal',
    hotkey: 'cmd+shift+t',
    action: { type: 'shell', command: 'cd ~/kube-coder && make python-tests' },
  },
  {
    id: 'seed-deploy',
    label: 'Deploy staging',
    icon: '🚀',
    action: { type: 'shell', command: 'cd ~/storefront && make deploy-staging' },
  },
  {
    id: 'seed-standup',
    label: 'Standup notes',
    icon: 'icon:memory',
    action: { type: 'task', prompt: 'Summarize yesterday’s commits into standup notes', workdir: '/home/dev' },
  },
  {
    id: 'seed-docs',
    label: 'Docs',
    icon: 'icon:docs',
    action: { type: 'url', url: 'https://github.com/imran31415/kube-coder#readme', target: 'blank' },
  },
];

export const mockApps: AppEntry[] = [
  {
    port: 3000,
    name: 'storefront',
    pinned: true,
    status: 'running',
    strip_prefix: false,
    addr: '127.0.0.1',
  },
  {
    port: 8080,
    name: '',
    pinned: false,
    status: 'running',
    strip_prefix: false,
    addr: '127.0.0.1',
  },
  {
    port: 5173,
    name: 'admin-ui',
    pinned: true,
    status: 'stopped',
    strip_prefix: true,
    addr: '127.0.0.1',
  },
];

// ---- Mission Control (issue #425) ------------------------------------------
// One unified queue across builds, chats and sub-agents. ref_ids reuse the
// mock task ids above so tapping a card lands on a populated detail screen.
// Pre-sorted by priority (waiting first), matching the server contract.

const mockMissionCards: MissionCard[] = [
  {
    id: 'build:d4e5f6',
    ref_id: 'd4e5f6',
    kind: 'build',
    state: 'waiting',
    title: 'Auth middleware refactor',
    headline: 'Found 3 duplicate Bearer checks — the shared helper changes the 401 body shape.',
    assistant: 'claude',
    model: 'fable-5',
    workdir: '/home/dev/kube-coder',
    repo: 'kube-coder',
    branch: 'refactor-auth-middleware',
    created_at: NOW - 600,
    updated_at: NOW - 40,
    finished_at: null,
    waiting_since: NOW - 840,
    waiting_prompt: {
      kind: 'choice',
      question: 'Proceed with the shared helper, or keep responses byte-identical?',
      options: [
        { index: 1, label: 'Shared helper' },
        { index: 2, label: 'Byte-identical' },
      ],
    },
    outcome: null,
    parent_id: null,
    children: [],
  },
  {
    id: 'build:a1b2c3',
    ref_id: 'a1b2c3',
    kind: 'build',
    state: 'running',
    title: 'Trigger run-history & audit log',
    headline: 'Wiring per-trigger history into server.py — writing the ring-buffer store.',
    assistant: 'claude',
    model: 'fable-5',
    workdir: '/home/dev/kube-coder',
    repo: 'kube-coder',
    branch: 'issue-91-trigger-history',
    created_at: NOW - 1920,
    updated_at: NOW - 5,
    finished_at: null,
    waiting_since: null,
    waiting_prompt: null,
    outcome: null,
    parent_id: null,
    children: [{ id: 'subagent:sa-test-writer', title: 'test-writer', state: 'running' }],
  },
  {
    id: 'subagent:sa-test-writer',
    ref_id: 'sa-test-writer',
    kind: 'subagent',
    state: 'running',
    title: 'test-writer',
    headline: 'Adding vitest coverage for webhook signature verification — 3 specs green.',
    assistant: 'codex',
    model: 'gpt-5.1-codex',
    workdir: '/home/dev/kube-coder',
    repo: 'kube-coder',
    branch: 'issue-91-trigger-history',
    created_at: NOW - 540,
    updated_at: NOW - 12,
    finished_at: null,
    waiting_since: null,
    waiting_prompt: null,
    outcome: null,
    parent_id: 'build:a1b2c3',
    children: [],
  },
  {
    id: 'chat:th-landing-copy',
    ref_id: 'th-landing-copy',
    kind: 'chat',
    state: 'running',
    title: 'Landing page copy refresh',
    headline: 'Iterating on pricing section wording — waiting on a slow Vite build.',
    assistant: 'claude',
    model: 'opus-4.8',
    workdir: '/home/dev/hosted',
    repo: 'hosted',
    branch: 'main',
    created_at: NOW - 7440,
    updated_at: NOW - 90,
    finished_at: null,
    waiting_since: null,
    waiting_prompt: null,
    outcome: null,
    parent_id: null,
    children: [],
  },
  {
    id: 'build:g7h8i9',
    ref_id: 'g7h8i9',
    kind: 'build',
    state: 'done',
    title: 'Webhook receiver integration tests',
    headline: '24 tests passing — branch pushed, PR ready for your review.',
    assistant: 'claude',
    model: 'fable-5',
    workdir: '/home/dev/api-service',
    repo: 'api-service',
    branch: 'webhook-tests',
    created_at: NOW - 3600,
    updated_at: NOW - 3000,
    finished_at: NOW - 3000,
    waiting_since: null,
    waiting_prompt: null,
    outcome: { ok: true, detail: '24 passed in 1.8s — HMAC, replay + oversized-body covered' },
    parent_id: null,
    children: [],
  },
  {
    id: 'build:j1k2l3',
    ref_id: 'j1k2l3',
    kind: 'build',
    state: 'done',
    title: 'Helm chart version bump',
    headline: 'Chart bumped to v1.12.1 and README table regenerated.',
    assistant: 'ante',
    model: 'qwen3-coder',
    workdir: '/home/dev/kube-coder',
    repo: 'kube-coder',
    branch: 'main',
    created_at: NOW - 7200,
    updated_at: NOW - 6800,
    finished_at: NOW - 6800,
    waiting_since: null,
    waiting_prompt: null,
    outcome: { ok: true, detail: 'helm lint clean; committed to main' },
    parent_id: null,
    children: [],
  },
  {
    id: 'build:m4n5o6',
    ref_id: 'm4n5o6',
    kind: 'build',
    state: 'done',
    title: 'Flaky terminal scroll test',
    headline: 'Repro attempt hit the CI timeout before the flake reproduced.',
    assistant: 'claude',
    model: 'fable-5',
    workdir: '/home/dev/kube-coder',
    repo: 'kube-coder',
    branch: 'main',
    created_at: NOW - 10800,
    updated_at: NOW - 10500,
    finished_at: NOW - 10500,
    waiting_since: null,
    waiting_prompt: null,
    outcome: { ok: false, detail: 'exited 1 — timed out after 30m without a repro' },
    parent_id: null,
    children: [],
  },
];

/** Fresh copies each call so screen-side mutation can't bleed into the seed. */
export function mockMissionQueue(): MissionQueue {
  const cards = mockMissionCards.map((c) => ({ ...c }));
  const oldestWait = cards
    .filter((c) => c.state === 'waiting' && c.waiting_since)
    .reduce((min, c) => Math.min(min, c.waiting_since ?? NOW), NOW);
  return {
    cards,
    pulse: {
      running: cards.filter((c) => c.state === 'running').length,
      waiting: cards.filter((c) => c.state === 'waiting').length,
      done_today: cards.filter((c) => c.state === 'done').length,
      oldest_wait_s: NOW - oldestWait,
      generated_at: NOW,
    },
  };
}

// ── Controller (admin plane) mocks ──────────────────────────────────────────

function mockWs(
  user: string,
  state: ControllerWorkspace['state'],
  isolated: boolean,
  detail: string,
  version: string | null,
  updateAvailable = false,
): ControllerWorkspace {
  return {
    user,
    deployment: `ws-${user}`,
    namespace: isolated ? `ws-${user}` : 'coder',
    isolated,
    state,
    desiredReplicas: state === 'stopped' ? 0 : 1,
    readyReplicas: state === 'running' ? 1 : 0,
    url: `https://${user}.kube-coder.app`,
    detail,
    version,
    updateAvailable,
  };
}

export const mockWorkspaces: ControllerWorkspace[] = [
  mockWs('imran', 'running', true, '1/1 ready', 'v1.11.0', true),
  mockWs('alex-worboys', 'running', true, '1/1 ready', 'v1.12.0'),
  mockWs('marketing-demo', 'stopped', false, 'scaled to 0', 'v1.9.0', true),
  mockWs('db-migration', 'transitioning', true, '0/1 starting', 'v1.12.0'),
  mockWs('nightly', 'degraded', true, 'CrashLoopBackOff', 'v1.10.0', true),
];

export const mockCapacity: ControllerCapacity = {
  generatedAt: NOW,
  namespace: 'coder',
  status: 'warn',
  metricsError: null,
  cluster: {
    nodeCount: 3,
    cpu: { clusterPct: 62.4, workspacePct: 38.1 },
    memory: { clusterPct: 78.9, workspacePct: 54.2 },
  },
};

// ---- Files -----------------------------------------------------------------
// A small in-memory /home/dev tree so the demo/screenshot build renders a
// populated Files screen with a working preview.
interface MockNode {
  entries?: Record<string, MockNode>; // present ⇒ directory
  content?: string; // present ⇒ text file
  image?: boolean; // ⇒ preview as image
  size?: number;
  mtime?: number;
}

const MOCK_TREE: Record<string, MockNode> = {
  'kube-coder': { entries: {}, mtime: NOW - 3600 },
  screenshots: {
    entries: {
      'dashboard.png': { image: true, size: 184320, mtime: NOW - 1800 },
    },
    mtime: NOW - 1800,
  },
  'notes.md': {
    content: '# Notes\n\n- Ship the Files manager\n- Verify preview + rename + delete\n',
    mtime: NOW - 600,
  },
  'server.log': {
    content: Array.from({ length: 40 }, (_, i) => `[info] line ${i + 1} — workspace healthy`).join('\n'),
    mtime: NOW - 120,
  },
};

function mockResolve(path: string): MockNode | null {
  const parts = path.split('/').filter(Boolean);
  let level: Record<string, MockNode> = MOCK_TREE;
  let node: MockNode | null = null;
  for (const part of parts) {
    node = level[part] ?? null;
    if (!node) return null;
    level = node.entries ?? {};
  }
  return node;
}

export function mockFileListing(path: string): FileListing {
  const level = path ? mockResolve(path)?.entries ?? {} : MOCK_TREE;
  const entries: FileEntry[] = Object.entries(level).map(([name, n]) => ({
    name,
    kind: n.entries ? 'dir' : 'file',
    size: n.size ?? (n.content ? n.content.length : 0),
    mtime: n.mtime ?? NOW,
  }));
  return { path, entries };
}

export function mockFilePreview(path: string): FilePreview {
  const node = mockResolve(path);
  const size = node?.size ?? (node?.content ? node.content.length : 0);
  if (node?.image) return { kind: 'image', path, mime: 'image/png', size };
  if (node?.content !== undefined) {
    return { kind: 'text', path, mime: 'text/plain', size, content: node.content, truncated: false };
  }
  return { kind: 'binary', path, mime: 'application/octet-stream', size };
}

// ---- Walkie-Talkie (internal loopback preview) -----------------------------
// A small in-memory transcript so the demo/screenshot build renders a populated
// Walkie screen, and send/control mutate it just enough to feel live. Mirrors
// the server's /api/gateway/internal/* shapes. Text/quick-reply only — no audio.
const MOCK_WALKIE_IDENTITY = 'internal:walkie';

function seedWalkieMessages(): PreviewMessage[] {
  return [
    {
      seq: 1,
      ts: NOW - 90,
      direction: 'out',
      kind: 'notice',
      text: '✅ Linked — send a message to talk to your workspace.',
      quick_replies: [],
      wire: null,
      meta: {},
    },
    {
      seq: 2,
      ts: NOW - 60,
      direction: 'in',
      kind: 'message',
      text: 'status',
      quick_replies: [],
      wire: { inbound: { from: MOCK_WALKIE_IDENTITY, text: 'status' } },
      meta: {},
    },
    {
      seq: 3,
      ts: NOW - 58,
      direction: 'out',
      kind: 'message',
      text: 'Workspace is healthy ✅\n• CPU 38%\n• Memory 54%\n• 2 running builds',
      quick_replies: ['recent builds', 'open desktop'],
      wire: {
        provider: 'internal',
        payloads: [
          {
            messaging_product: 'internal',
            to: MOCK_WALKIE_IDENTITY,
            type: 'interactive',
            interactive: {
              type: 'button',
              body: { text: 'Workspace is healthy ✅' },
              action: {
                buttons: [
                  { type: 'reply', reply: { id: 'recent builds', title: 'recent builds' } },
                  { type: 'reply', reply: { id: 'open desktop', title: 'open desktop' } },
                ],
              },
            },
          },
        ],
      },
      meta: {},
    },
  ];
}

let mockWalkieMessages: PreviewMessage[] = seedWalkieMessages();
let mockWalkieLinked = true;
let mockWalkieSimulate = false;

function mockWalkieCursor(): number {
  return mockWalkieMessages.length ? mockWalkieMessages[mockWalkieMessages.length - 1].seq : 0;
}

function pushWalkie(m: Omit<PreviewMessage, 'seq' | 'ts'>): void {
  mockWalkieMessages.push({ ...m, seq: mockWalkieCursor() + 1, ts: Math.floor(Date.now() / 1000) });
}

/** Full preview state (the web + mobile screens fetch since=0 and replace). */
export function mockPreviewState(): PreviewState {
  return {
    available: true,
    messages: mockWalkieMessages.map((m) => ({ ...m })),
    cursor: mockWalkieCursor(),
    linked: mockWalkieLinked,
    simulate_out_of_window: mockWalkieSimulate,
    provider: 'internal',
    identity: MOCK_WALKIE_IDENTITY,
    busy: false,
    thread_id: 'mock-thread',
  };
}

/** Record an inbound (typed text or a tapped quick-reply) + a canned reply. */
export function mockPreviewSend(text: string, button?: string): PreviewSendResult {
  const display = (button ?? text).trim();
  pushWalkie({
    direction: 'in',
    kind: 'message',
    text: display,
    quick_replies: [],
    wire: { inbound: { from: MOCK_WALKIE_IDENTITY, text, button: button ?? '' } },
    meta: {},
  });
  pushWalkie({
    direction: 'out',
    kind: mockWalkieSimulate ? 'template' : 'message',
    text: `You said “${display}”. (demo reply — connect a workspace for a real agent turn.)`,
    quick_replies: ['status', 'recent builds'],
    wire: {
      provider: 'internal',
      payloads: [{ messaging_product: 'internal', to: MOCK_WALKIE_IDENTITY, type: 'text', text: { body: display } }],
    },
    meta: {},
  });
  return { ok: true, action: 'reply', cursor: mockWalkieCursor() };
}

/** Link / toggle out-of-window simulation / reset — mirrors the control API. */
export function mockPreviewControl(action: PreviewControlAction, on?: boolean): PreviewControlResult {
  if (action === 'link') {
    mockWalkieLinked = true;
    return { ok: true, linked: true };
  }
  if (action === 'simulate') {
    mockWalkieSimulate = !!on;
    return { ok: true, simulate_out_of_window: mockWalkieSimulate };
  }
  // reset
  mockWalkieMessages = seedWalkieMessages();
  mockWalkieSimulate = false;
  return { ok: true };
}
