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
  DesktopItem,
  Health,
  MemoryRecord,
  Metrics,
  TaskDetail,
  TaskSummary,
} from '../api/types';

const NOW = Math.floor(Date.now() / 1000);

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
