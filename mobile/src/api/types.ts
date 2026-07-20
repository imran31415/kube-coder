/** Shared API types mirroring server.py's JSON shapes. */

export interface TaskSummary {
  id: string;
  prompt: string;
  status: string; // running | waiting | done | error | killed
  assistant?: string;
  workdir?: string;
  created_at?: number;
  updated_at?: number;
  waiting_for_input?: boolean;
}

export interface TaskDetail extends TaskSummary {
  output?: string;
  tmux_session?: string;
}

export interface MemoryRecord {
  namespace: string;
  key: string;
  value: string;
  tags?: string[];
  importance?: number;
  updated_at?: number;
}

/** A normalized agent skill discovered from any harness (issue #187).
 *  `systems` lists every harness (claude/opencode/…) sharing this content. */
export interface SkillRecord {
  name: string;
  description: string;
  body?: string;
  scope: string; // project | user | plugin
  systems: string[];
  user_invocable?: boolean;
  allowed_tools?: string[];
  argument_hint?: string;
  updated_at?: number;
}

/** File-manager entries (mirror /api/files/list on server.py). */
export interface FileEntry {
  name: string;
  kind: 'dir' | 'file';
  size: number;
  mtime: number;
}
export interface FileListing {
  path: string;
  entries: FileEntry[];
}

/** Preview descriptor from /api/files/preview. Text carries capped content;
 *  image/video signal an inline render via /api/files/raw; binary → download. */
export type FilePreview =
  | { kind: 'text'; path: string; mime: string; size: number; content: string; truncated: boolean }
  | { kind: 'image'; path: string; mime: string; size: number }
  | { kind: 'video'; path: string; mime: string; size: number }
  | { kind: 'binary'; path: string; mime: string; size: number; reason?: string };

export interface Metrics {
  cpu_percent: number;
  memory_used_mb: number;
  memory_total_mb: number;
  disk_used_gb: number;
  disk_total_gb: number;
}

export interface Health {
  vscode?: boolean;
  terminal?: boolean;
  browser?: boolean;
  ok?: boolean;
}

/** Desktop launcher (mirrors /api/desktop — the web dashboard's Desktop tab). */
export type DesktopActionType = 'task' | 'url' | 'shell';

export interface DesktopActionTask {
  type: 'task';
  prompt: string;
  workdir?: string;
  assistant?: string;
}
export interface DesktopActionUrl {
  type: 'url';
  url: string;
  target: 'blank' | 'self';
}
export interface DesktopActionShell {
  type: 'shell';
  command: string;
  timeout?: number;
}
export type DesktopAction = DesktopActionTask | DesktopActionUrl | DesktopActionShell;

export interface DesktopItem {
  id: string;
  label: string;
  /** Emoji/text, or "icon:NAME" for a named line icon. */
  icon: string;
  /** Web-only keyboard shortcut; preserved (not editable) on mobile. */
  hotkey?: string;
  action: DesktopAction;
}

export type DesktopItemDraft = Omit<DesktopItem, 'id'>;

export type LaunchResult =
  | { kind: 'task'; task_id: string }
  | { kind: 'shell'; exit_code: number; stdout: string; stderr: string }
  | { kind: 'url'; url: string; target: 'blank' | 'self' };

/** One entry on the Applications tab (mirrors /api/apps). */
export interface AppEntry {
  port: number;
  /** Pinned name, or '' for an auto-discovered listener. */
  name: string;
  pinned: boolean;
  /** running (listening now) | stopped (pinned, not listening) | blocked (reserved port). */
  status: 'running' | 'stopped' | 'blocked';
  strip_prefix: boolean;
  /** Bind address from /proc/net/tcp, e.g. "127.0.0.1". */
  addr: string;
}

// ── Controller (admin plane) ────────────────────────────────────────────────
// Mirrors the workspace-controller's /api/workspaces and /api/capacity/summary.

export type WorkspaceState = 'running' | 'stopped' | 'transitioning' | 'degraded';

/** One workspace in the controller's list (mirrors controller.py list_workspaces). */
export interface ControllerWorkspace {
  user: string;
  deployment: string;
  namespace: string;
  isolated: boolean;
  state: WorkspaceState;
  desiredReplicas: number;
  readyReplicas: number;
  url: string | null;
  /** Short human status, e.g. "1/1 ready" or "CrashLoopBackOff". */
  detail: string;
  version: string | null;
  updateAvailable: boolean;
}

export interface ControllerWorkspacesResponse {
  namespace: string;
  workspaces: ControllerWorkspace[];
  latestVersion: string | null;
}

/** One resource rollup in the capacity summary (percent-of-cluster). */
export interface CapacityResource {
  clusterPct: number | null;
  workspacePct: number | null;
}

/** Cheap cluster-health rollup (mirrors controller.py cluster_health). */
export interface ControllerCapacity {
  generatedAt: number;
  namespace: string;
  status: 'ok' | 'warn' | 'critical' | 'unknown';
  metricsError: string | null;
  cluster: {
    nodeCount: number;
    cpu: CapacityResource;
    memory: CapacityResource;
  } | null;
}

// ---- Hypervisor chat -------------------------------------------------------
// A thread is a structured agent session; the server returns a canonical event
// stream (see charts/workspace/hypervisor_session.py) the app renders directly.
export type HvEventRole = 'user' | 'assistant' | 'system';
export type HvEventType = 'message' | 'tool_call' | 'tool_result' | 'error' | 'status' | 'choice';

export interface HvEvent {
  seq: number;
  ts: number;
  role: HvEventRole;
  type: HvEventType;
  text?: string;
  tool?: { name: string; input: unknown };
  tool_id?: string;
  tool_use_id?: string;
  is_error?: boolean;
  status?: string;
  // choice events: a multiple-choice prompt (backend splits a ```choice fence
  // into this — see hypervisor_session.py). Rendered as tappable buttons.
  options?: string[];
  question?: string;
}

export interface HypervisorAssistant {
  id: string;
  label: string;
  default?: boolean;
  model?: string;
}

export interface HypervisorConfig {
  enabled: boolean;
  defaultAssistant: string;
  workdir: string;
  readOnly: boolean;
  assistants: HypervisorAssistant[];
}

export interface HypervisorThread {
  id: string;
  title: string;
  assistant: string | null;
  status: string;
  created_at: number | null;
  updated_at: number | null;
  // Present (unix seconds) only on soft-deleted threads in the trash view.
  deleted_at?: number | null;
}

// Where the rendered transcript came from — 'session_log' (Claude Code's own
// JSONL log) or 'capture' (the live events.jsonl fallback). A flip re-stamps
// event seqs, so the poll guard treats it as a content change.
export type TranscriptSource = 'session_log' | 'capture';

export interface HypervisorThreadDetail {
  thread: HypervisorThread;
  events: HvEvent[];
  source?: TranscriptSource;
}

// ---- Walkie-Talkie (internal loopback preview) -----------------------------
// Mirrors charts/workspace/web/src/api/gatewayPreview.ts and the backend
// /api/gateway/internal/* handlers (server.py). An internal loopback channel:
// messages run through the real Conversation Gateway core and come back as chat
// bubbles. Only the internal loopback transport is connected today; other
// providers will be added soon. Text/quick-reply only — no device audio.
export interface PreviewWire {
  provider?: string;
  /** Outbound provider message objects, as they'd hit the wire. */
  payloads?: unknown[];
  /** Inbound provider webhook shape. */
  inbound?: Record<string, unknown>;
  error?: string;
}

export type PreviewDirection = 'in' | 'out';
export type PreviewKind = 'message' | 'template' | 'notice';

export interface PreviewMessage {
  seq: number;
  ts: number;
  direction: PreviewDirection;
  kind: PreviewKind;
  text: string;
  quick_replies: string[];
  wire: PreviewWire | null;
  meta: Record<string, unknown>;
}

export interface PreviewState {
  available: boolean;
  messages: PreviewMessage[];
  cursor: number;
  linked: boolean;
  simulate_out_of_window: boolean;
  provider: string;
  identity: string;
  busy: boolean;
  thread_id: string | null;
}

export interface PreviewSendResult {
  ok: boolean;
  action: string;
  cursor: number;
}

export type PreviewControlAction = 'link' | 'simulate' | 'reset';

export interface PreviewControlResult {
  ok: boolean;
  linked?: boolean;
  simulate_out_of_window?: boolean;
}
