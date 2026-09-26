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
  Project,
  ProjectBrief,
  FeedItem,
  HypervisorThread,
  CronRecord,
  PageWatchRecord,
  DocsManifest,
  DocsPage,
  WebhookRecord,
  BoardStanding,
  BoardSummary,
  BoardReviewItem,
  TaskWorktreeView,
  WorktreeDiff,
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
    workdir: '/home/dev/.worktrees/kube-coder/t-a1b2c3',
    created_at: NOW - 90,
    updated_at: NOW - 5,
    // An isolated Build (#701), so the mock shows the worktree row.
    worktree: {
      branch: 'kc/t-a1b2c3',
      port: 3101,
      path: '/home/dev/.worktrees/kube-coder/t-a1b2c3',
      removed: false,
      stat: { files_changed: 2, insertions: 41, deletions: 3, ahead: 1, dirty: 1, untracked: 0 },
    },
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

/** GET /api/claude/tasks/<id>/worktree for the mock isolated Build (#701). */
export function mockWorktreeView(id: string): TaskWorktreeView | null {
  const t = mockTasks.find((x) => x.id === id);
  if (!t?.worktree?.path) return null;
  return {
    task_id: id,
    worktree: {
      path: t.worktree.path,
      branch: t.worktree.branch ?? '',
      port: t.worktree.port ?? null,
      base_ref: 'main',
      base_sha: '4f1c2a9d0e7b3c5a8f6d1e2b9c0a7d3e5f4b1c2a',
      repo_root: '/home/dev/kube-coder',
    },
    exists: true,
    branch_exists: true,
    live: t.status === 'running' || t.status === 'waiting',
    status: {
      branch: t.worktree.branch ?? '',
      behind: 0,
      files_changed: 2, insertions: 41, deletions: 3, ahead: 1, dirty: 1, untracked: 0,
      files: [
        { path: 'charts/workspace/server.py', status: 'M', added: 18, deleted: 3, binary: false, uncommitted: true },
        { path: 'charts/workspace/tests/healthz_test.py', status: 'A', added: 23, deleted: 0, binary: false, uncommitted: false },
      ],
      truncated: false,
    },
    status_error: '',
    push_command: `git -C ${t.worktree.path} push -u fork ${t.worktree.branch}`,
    remove_blocked: t.status === 'running' || t.status === 'waiting' ? 'live' : '',
  };
}

export function mockWorktreeDiff(file: string): WorktreeDiff {
  return {
    file,
    binary: false,
    truncated: false,
    diff: `--- a/${file}
+++ b/${file}
@@ -1,3 +1,6 @@
 import os
+
+def healthz():
+    return {'ok': True}
`,
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
    evidence: [],
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
    evidence: [],
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
    evidence: [],
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
    evidence: [],
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
    evidence: [
      { label: 'vitest 24', ok: true, link: null },
      { label: 'PR #418', ok: null, link: 'https://github.com/imran31415/api-service/pull/418' },
    ],
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
    evidence: [],
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
    evidence: [],
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

// ---- AI CTO / Projects + Feed (#468) — demo fixtures -----------------------

export const mockProjects: Project[] = [
  {
    id: 'kube-coder', name: 'kube-coder', workdirs: ['/home/dev/kube-coder'],
    repo: 'imran31415/kube-coder', memory_namespace: 'project.kube-coder',
    status: 'active', north_star: 'Ship the AI CTO to every workspace',
    last_seen_at: NOW - 90000, created_at: NOW - 9e5, updated_at: NOW - 200,
    pulse: { running: 2, waiting: 1, last_activity_at: NOW - 200 },
  },
  {
    id: 'hosted', name: 'hosted', workdirs: ['/home/dev/hosted'],
    repo: 'imran31415/kubecoder-hosted', memory_namespace: 'project.hosted',
    status: 'active', north_star: 'KubeCoder.com landing + waitlist',
    last_seen_at: NOW - 4e5, created_at: NOW - 8e5, updated_at: NOW - 6e4,
    pulse: { running: 0, waiting: 0, last_activity_at: NOW - 6e4 },
  },
];

export function mockProjectBrief(id: string): ProjectBrief {
  const project = mockProjects.find((p) => p.id === id) ?? mockProjects[0];
  return {
    project,
    tasks: {
      running: 2, waiting: 1, total: 7,
      recent: [
        { task_id: 'a1b2c3', status: 'running', prompt: 'Build the mobile CTO screen', workdir: project.workdirs[0], assistant: 'claude', last_activity_at: NOW - 200 },
        { task_id: 'd4e5f6', status: 'waiting', prompt: 'Confirm the feed schema', workdir: project.workdirs[0], assistant: 'codex', last_activity_at: NOW - 1800 },
      ],
    },
    goals: [
      { namespace: 'project.kube-coder.goals', key: 'ga', value: 'Reach GA with the CTO page + feed', tags: ['goal'], importance: 0.9, updated_at: NOW - 8000 },
    ],
    decisions: [
      { namespace: 'project.kube-coder.decisions', key: 'sse', value: 'SSE over websockets — matches the rest of the stack', tags: ['decision'], importance: 0.8, updated_at: NOW - 40 },
      { namespace: 'project.kube-coder.decisions', key: 'no-fork', value: 'Reuse the hypervisor chat via a store context, do not fork it', tags: ['decision'], importance: 0.8, updated_at: NOW - 120000 },
    ],
    memories: [],
    git: [{ workdir: project.workdirs[0], branch: 'feat/471-mobile-cto-feed', exists: true }],
    triggers: [],
    counts: { goals: 1, decisions: 2, memories: 0, tasks: 7 },
    brief_markdown: `# ${project.name} — project brief`,
  };
}

/** One chat list for every mode (#683) — a plain workspace chat, a CTO one and
 *  a Board Processor one, so the mock exercises the mode badges the merged list
 *  renders. Before the merge the CTO thread was the only mocked thread, and it
 *  was reachable only through `persona=cto`. */
export function mockThreads(): HypervisorThread[] {
  return [
    { id: 'th-1', title: 'Ship the chart change', assistant: 'claude', status: 'idle', created_at: NOW - 9000, updated_at: NOW - 120, persona: '', project_id: 'kube-coder' },
    { id: 'th-cto-1', title: 'What should I focus on?', assistant: 'claude', status: 'idle', created_at: NOW - 5000, updated_at: NOW - 300, persona: 'cto', project_id: 'kube-coder' },
    { id: 'th-board-1', title: 'Triage KC-214', assistant: 'claude', status: 'idle', created_at: NOW - 40000, updated_at: NOW - 20000, persona: 'board', project_id: 'kube-coder' },
  ];
}

export function mockFeed(): FeedItem[] {
  return [
    {
      id: 'fd_1', ts: NOW - 200, kind: 'briefing', title: 'Morning briefing — the release is the bottleneck',
      body_md: 'Two PRs are green and waiting on review; the feed backend is the critical path.',
      source: 'agent:th-cto-1', project_id: 'kube-coder',
      links: [{ label: 'Open thread', ref: 'thread:th-cto-1' }], waiting: false, read: false,
    },
    {
      id: 'fd_2', ts: NOW - 1800, kind: 'news', title: 'brace-expansion advisory affects minimatch < 10',
      body_md: 'A dep-scout run flagged `GHSA-mh99-v99m-4gvg`. Your lockfile pins minimatch ^10 — **no action needed**.',
      source: 'cron:dep-scout', project_id: 'kube-coder',
      links: [{ label: 'Advisory', href: 'https://github.com/advisories/GHSA-mh99-v99m-4gvg' }], waiting: false, read: false,
    },
    {
      id: 'fd_3', ts: NOW - 5400, kind: 'activity', title: 'Task waiting on you: confirm the feed schema',
      body_md: '', source: 'system:task', project_id: 'kube-coder',
      links: [{ label: 'Open task', ref: 'task:d4e5f6' }], waiting: true, read: true,
    },
    {
      // The demo build's only board deep link. It points at the SECOND board
      // and at an item id that contains a colon — a GitHub GraphQL global id —
      // so following it exercises the board switch and the ref split, not the
      // trivial case where the target is already on screen (#692).
      id: 'fd_5', ts: NOW - 2700, kind: 'activity', title: 'kube-coder#4102 needs your review',
      body_md: 'reproduced on 1.60.2; the patch is a one-line guard',
      source: 'board:kube-coder-gh', project_id: 'kube-coder',
      links: [{ label: 'Board runs OOM a 4GiB workspace', ref: 'board:kube-coder-gh:I_kwDOA:4102' }],
      waiting: true, read: false,
    },
    {
      id: 'fd_4', ts: NOW - 90000, kind: 'decision', title: 'SSE over websockets — matches the rest of the stack',
      body_md: '', source: 'system:memory', project_id: 'kube-coder',
      links: [{ label: 'View decision', ref: 'memory:project.kube-coder.decisions/sse' }], waiting: false, read: true,
    },
  ];
}

// ---- Triggers (#250) -------------------------------------------------------
// Mutable, so the demo build's create/delete/pause actions actually do
// something (same approach as mockDesktop).

export const mockWebhooks: WebhookRecord[] = [
  {
    id: 'github-ci',
    prompt_template: 'A GitHub workflow failed. Investigate the run and propose a fix.',
    workdir: '/home/dev/kube-coder',
    interpolate_mode: 'attach',
    provider: 'github',
    created_at: NOW - 86400 * 6,
    hmac_secret_set: true,
    unsigned: false,
  },
  {
    id: 'deploy-hook',
    prompt_template: 'Smoke-test the staging deploy and report anything red.',
    workdir: '/home/dev',
    interpolate_mode: 'attach',
    provider: 'generic',
    created_at: NOW - 86400 * 19,
    hmac_secret_set: true,
    unsigned: false,
  },
];

// Page watches (#681). Three states worth seeing in demo mode: healthy,
// never-checked, and failing — the row renders differently for each.
export const mockPageWatches: PageWatchRecord[] = [
  {
    id: 'ci-green',
    url: 'https://github.com/imran31415/kube-coder/actions/workflows/ci.yml/badge.svg',
    selector: 'text',
    schedule: '*/5 * * * *',
    prompt_template: 'CI finished. Summarise the result and tell me if anything needs attention.',
    workdir: '/home/dev/kube-coder',
    timezone: 'UTC',
    include_content: false,
    render: false,
    suspended: false,
    created_at: NOW - 86400 * 2,
    fire_token_set: true,
    last_hash: 'sha256:9f2c1ab4',
    last_checked_at: NOW - 240,
    last_changed_at: NOW - 3600 * 5,
    last_error: null,
    consecutive_failures: 0,
  },
  {
    id: 'release-notes',
    url: 'https://example.com/releases',
    selector: null,
    schedule: '0 * * * *',
    prompt_template: 'A new release was published — draft an upgrade note for the team.',
    workdir: '/home/dev',
    timezone: 'UTC',
    include_content: true,
    render: false,
    suspended: false,
    created_at: NOW - 600,
    fire_token_set: true,
    last_hash: null,
    last_checked_at: null,
    last_changed_at: null,
    last_error: null,
    consecutive_failures: 0,
  },
  {
    id: 'status-page',
    url: 'https://status.example.com/',
    selector: '.incident',
    schedule: '*/10 * * * *',
    prompt_template: 'The status page changed — check whether it affects our deploys.',
    workdir: '/home/dev',
    timezone: 'UTC',
    include_content: false,
    render: false,
    suspended: true,
    created_at: NOW - 86400 * 6,
    fire_token_set: true,
    last_hash: 'sha256:11aa22bb',
    last_checked_at: NOW - 1800,
    last_changed_at: NOW - 86400,
    last_error: "the selector '.incident' matched nothing on this page",
    consecutive_failures: 3,
  },
];

export const mockCrons: CronRecord[] = [
  {
    id: 'dep-scout',
    schedule: '0 9 * * *',
    prompt_template: 'Scan the lockfiles for new advisories and post anything actionable to the feed.',
    workdir: '/home/dev/kube-coder',
    timezone: 'America/Los_Angeles',
    suspended: false,
    created_at: NOW - 86400 * 11,
    fire_token_set: true,
  },
  {
    id: 'nightly-tests',
    schedule: '30 2 * * *',
    prompt_template: 'Run the full test suite and open an issue for any new failure.',
    workdir: '/home/dev/kube-coder',
    timezone: 'UTC',
    suspended: true,
    created_at: NOW - 86400 * 31,
    fire_token_set: true,
  },
];

// ---- Docs (#250) -----------------------------------------------------------
// A trimmed stand-in for docs/_manifest.json plus a couple of real-ish pages,
// so the demo build renders a browsable docs surface with no workspace.

export const mockDocsManifest: DocsManifest = {
  version: 1,
  sections: [
    {
      id: 'overview',
      title: 'Overview',
      pages: [
        {
          id: 'getting-started',
          title: 'Getting started',
          file: 'in-app/getting-started.md',
          summary: 'Connect the app to your workspace, land in the AI CTO, and ship your first build.',
        },
      ],
    },
    {
      id: 'tasks',
      title: 'Tasks',
      pages: [
        {
          id: 'tasks-concepts',
          title: 'Concepts',
          file: 'in-app/tasks-concepts.md',
          summary: 'What a build is, its lifecycle states, tmux sessions, and attaching to one.',
        },
        {
          id: 'tasks-api',
          title: 'HTTP API',
          file: 'claude-task-api.md',
          summary: 'The Claude Task API reference — curl recipes and the endpoint matrix.',
        },
      ],
    },
    {
      id: 'triggers',
      title: 'Triggers',
      pages: [
        {
          id: 'triggers-webhooks',
          title: 'Webhooks',
          file: 'in-app/triggers-webhooks.md',
          summary: 'Signed webhooks, payload templating, response URLs, and replay protection.',
        },
      ],
    },
    {
      id: 'memory',
      title: 'Memory',
      pages: [
        {
          id: 'memory-concepts',
          title: 'Concepts',
          file: 'in-app/memory-concepts.md',
          summary: 'Namespaces, importance, tags, and how the agent reads memory back.',
        },
      ],
    },
  ],
};

const MOCK_DOC_BODIES: Record<string, string> = {
  'getting-started': `# Getting started

Your workspace is a container in Kubernetes with a persistent volume. This app
talks to it over the same HTTP API the dashboard uses.

## Connect

1. Open **Settings → Connection** on the workspace dashboard.
2. Copy the workspace URL and API token.
3. Paste both into the app's onboarding screen.

> The token is stored in the device keychain, never in plain storage.

## Your first build

Type one sentence into the composer on the **Desktop** home — "add a /healthz
endpoint and a unit test" — and hit build. The agent runs in a tmux session on
the pod, so closing the app never kills the work.

\`\`\`bash
curl -H "Authorization: Bearer $TOKEN" \\
  https://your-workspace.example.com/api/claude/tasks
\`\`\`
`,
  'tasks-concepts': `# Concepts

A **build** is one Claude Code session running in its own tmux window.

- **running** — the agent is working.
- **waiting** — it asked you something and is blocked.
- **done** — the session exited cleanly.
- **error** — the session exited non-zero.

Attach from a terminal with \`tmux attach -t claude-<id>\`. State lives under
\`~/.claude-tasks/\`, on the persistent volume, so a pod restart never loses it.
`,
  'tasks-api': `# HTTP API

All endpoints take \`Authorization: Bearer <token>\`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | /api/claude/tasks | Create a task |
| GET | /api/claude/tasks | List tasks |
| GET | /api/claude/tasks/{id} | Task detail + output |
| DELETE | /api/claude/tasks/{id} | Kill a task |
`,
  'triggers-webhooks': `# Webhooks

A webhook turns an inbound HTTP POST into a build.

## Signing

Every webhook is created with an HMAC secret. Unsigned requests are rejected —
the receiver *fails closed*, so a secret-less webhook accepts nothing at all.

1. Copy the secret shown once at create time.
2. Paste it into the sending service (GitHub, Stripe, …).
3. The sender signs each body; the workspace verifies before spawning anything.

## Templating

The request body is attached to the prompt by default. Switch to
**interpolate** mode to substitute \`{{ fields }}\` into the prompt instead.
`,
  'memory-concepts': `# Memory concepts

Memory is a SQLite store on the persistent volume, exposed to agents over MCP.

- **namespace** — \`user.*\` for personal facts, \`project.<repo>.*\` for project facts.
- **importance** — 0 to 1; higher entries win a slot in the prompt injection.
- **tags** — tag an entry \`secret\` to keep it out of the auto-injected block.

Ask the agent to "remember" something and it lands here; the Memory tab is a
browsable view of the same table.
`,
};

export function mockDocsPage(id: string): DocsPage | null {
  for (const section of mockDocsManifest.sections) {
    const page = section.pages.find((p) => p.id === id);
    if (!page) continue;
    return {
      id: page.id,
      title: page.title,
      summary: page.summary,
      section_id: section.id,
      section_title: section.title,
      file: page.file,
      edited_at: NOW - 86400 * 3,
      markdown: MOCK_DOC_BODIES[id] ?? `# ${page.title}\n\n${page.summary ?? ''}\n`,
    };
  }
  return null;
}

// ---- Board Processor review (#588 Phase 6) ---------------------------------
// EXPO_PUBLIC_MOCK=1 powers the screenshot and web-export flows, and every
// client function short-circuits on getConfig().mock — so a screen with no
// mock data renders empty in every screenshot. Writes must MUTATE these
// module-level arrays, or approving in mock mode looks like a no-op.

export const mockBoards: BoardSummary[] = [
  { id: 'acme-jira', display_name: 'Acme — Support', vendor: 'jira', credential_set: true },
  { id: 'kube-coder-gh', display_name: 'kube-coder issues', vendor: 'github', credential_set: true },
];

export const mockBoardReview: BoardReviewItem[] = [
  {
    board_id: 'acme-jira',
    item_id: '812',
    item_key: 'SUP-812',
    item_title: 'Refund not received',
    item_url: 'https://acme.atlassian.net/browse/SUP-812',
    content_hash: 'ab12cd34',
    task_id: 'a1b2c3',
    // Worked in its own worktree (#701) — the review card shows the branch.
    worktree: {
      task_id: 'a1b2c3',
      branch: 'kc/b-acme-jira-812-5e1f0a2b',
      port: 3102,
      path: '/home/dev/.worktrees/kube-coder/b-acme-jira-812-5e1f0a2b',
      removed: false,
      stat: { files_changed: 2, insertions: 41, deletions: 3, ahead: 1, dirty: 0, untracked: 0 },
    },
    state: 'pending',
    disposition: 'needs_review',
    reason: 'matched refund txn 8821 in Stripe; no further action needed',
    evidence: { tool_calls: 3, tokens: '12k' },
    actions: [],
    pending_actions: [
      {
        id: 'a1',
        action: 'comment',
        params: { body: 'Hi Dana — I confirmed the refund was issued on the 3rd.' },
        preview: 'Hi Dana — I confirmed the refund was issued on the 3rd.',
        writes: 1,
        state: 'pending',
      },
      {
        id: 'a2',
        action: 'set_status',
        params: { status: 'Done' },
        preview: 'IN_PROGRESS → CLOSED',
        writes: 1,
        state: 'pending',
      },
    ],
    open: true,
    decided_by: '',
    created_at: NOW - 3600 * 3,
    updated_at: NOW - 3600 * 3,
  },
  {
    board_id: 'kube-coder-gh',
    // A GitHub GraphQL global id: it contains a colon, which is exactly what
    // the "board:<board_id>:<item_id>" ref has to survive.
    item_id: 'I_kwDOA:4102',
    item_key: '#4102',
    item_title: 'Board runs OOM a 4GiB workspace',
    item_url: 'https://github.com/imran31415/kube-coder/issues/4102',
    content_hash: 'cd90ef12',
    state: 'pending',
    disposition: 'needs_review',
    reason: 'reproduced on 1.60.2; concurrent agents exceed the memory limit',
    evidence: { tool_calls: 7, tests: '18 passed' },
    actions: [],
    pending_actions: [
      {
        id: 'a3',
        action: 'comment',
        params: { body: 'Reproduced — the default concurrency of 3 exceeds a 4GiB limit. Guarding it in #4110.' },
        preview: 'Reproduced — the default concurrency of 3 exceeds a 4GiB limit. Guarding it in #4110.',
        writes: 1,
        state: 'pending',
      },
    ],
    open: true,
    decided_by: '',
    created_at: NOW - 3600 * 5,
    updated_at: NOW - 3600 * 5,
  },
  {
    board_id: 'acme-jira',
    item_id: '815',
    item_key: 'SUP-815',
    item_title: 'Cannot log in after password reset',
    item_url: 'https://acme.atlassian.net/browse/SUP-815',
    content_hash: 'ef56ab78',
    state: 'pending',
    disposition: 'needs_rescoping',
    reason: 'two accounts share this email; which one should be reset?',
    evidence: { tool_calls: 5 },
    actions: [],
    pending_actions: [],
    open: true,
    decided_by: '',
    created_at: NOW - 3600 * 9,
    updated_at: NOW - 3600 * 9,
  },
];

/**
 * Board standing for the demo build (#712).
 *
 * Computed from `mockBoardReview` rather than hard-coded, so approving the
 * last open card in the screenshot flow moves the badge from "Waiting on you"
 * to "Idle" the way the real endpoint would. The first board is shown mid-run
 * so the screenshots carry both states.
 */
export function mockBoardStanding(boardId: string): BoardStanding {
  const board = mockBoards.find((b) => b.id === boardId);
  const awaiting = mockBoardReview.filter(
    (i) => i.board_id === boardId && i.open,
  ).length;
  const live = boardId === 'acme-jira';
  const state: BoardStanding['state'] = live
    ? 'running'
    : awaiting > 0
      ? 'awaiting_human'
      : 'idle';
  const label = {
    running: 'Runs in progress',
    awaiting_human: 'Waiting on you',
    idle: 'Idle',
    never_run: 'Not run yet',
    needs_credential: 'Needs a credential',
  }[state];
  return {
    board_id: boardId,
    display_name: board?.display_name || boardId,
    state,
    label,
    detail: live
      ? `4/9 worked · 2 working · 3 queued${awaiting ? ` · ${awaiting} awaiting you` : ''}`
      : awaiting > 0
        ? `${awaiting} ${awaiting === 1 ? 'item is' : 'items are'} waiting on your decision.`
        : 'Everything worked so far has been decided.',
    live,
    run_id: live ? 'run-1756000000-ab12' : '',
    mode: 'propose',
    working: live ? 2 : 0,
    queued: live ? 3 : 0,
    settled: live ? 4 : 0,
    run_total: live ? 9 : 0,
    awaiting,
    runs: live ? 3 : 1,
    can_start_run: !live,
    blocked_reason: live
      ? 'A run is already in flight on this board (run-1756000000-ab12). Stop it, or wait for it to finish — a second run can only skip the items this one holds.'
      : '',
  };
}

/** Mock write: mutate in place so the screen actually changes. */
export function mockDecideBoardItem(itemId: string, state: BoardReviewItem['state']): void {
  const item = mockBoardReview.find((i) => i.item_id === itemId);
  if (!item) return;
  item.state = state;
  item.open = false;
  item.decided_by = 'dashboard:you@example.com';
  for (const action of item.pending_actions) {
    action.state = state === 'approved' ? 'done' : 'discarded';
  }
  item.actions = item.pending_actions;
  item.pending_actions = [];
}
