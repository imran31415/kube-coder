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

/** A candidate working directory from /api/workspace/dirs — the top-level
 *  folders under /home/dev, newest-touched first (same source as the web
 *  pickers). Note the server field is `is_git_repo`, not the web SPA's
 *  optimistic `is_git`. */
export interface WorkdirOption {
  path: string;
  label?: string;
  is_git_repo?: boolean;
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

// ---- Mission Control (issue #425) ------------------------------------------
// One unified queue of agent work — builds, hypervisor chats and sub-agents —
// mirroring GET /api/missioncontrol/queue. Cards arrive pre-sorted by priority
// (waiting first); timestamps are unix seconds.

export type MissionCardKind = 'build' | 'chat' | 'subagent';
export type MissionCardState = 'running' | 'waiting' | 'done';

export interface MissionWaitingOption {
  index: number | string;
  label: string;
}

/** The question the agent is blocked on, when state === 'waiting'. */
export interface MissionWaitingPrompt {
  kind: 'choice' | 'yesno';
  question: string | null;
  options: MissionWaitingOption[];
}

export interface MissionCardChild {
  id: string;
  title: string;
  state: string;
}

/** One evidence chip parsed from a finished task's output tail — a test
 *  tally, tsc errors, or a PR link. `ok` null renders neutral (a link). */
export interface MissionEvidence {
  label: string;
  ok: boolean | null;
  link: string | null;
}

export interface MissionCard {
  /** Namespaced id, e.g. "build:<id>" | "chat:<id>" | "subagent:<id>". */
  id: string;
  /** Raw id inside the kind's own namespace (task id / thread id). */
  ref_id: string;
  kind: MissionCardKind;
  state: MissionCardState;
  title: string;
  headline: string;
  assistant: string | null;
  model: string;
  workdir: string;
  repo: string;
  branch: string;
  created_at: number | null;
  updated_at: number | null;
  finished_at: number | null;
  waiting_since: number | null;
  waiting_prompt: MissionWaitingPrompt | null;
  outcome: { ok: boolean; detail: string } | null;
  /** Chips on Done cards (empty for running/waiting cards and chats). */
  evidence: MissionEvidence[];
  parent_id: string | null;
  children: MissionCardChild[];
}

/** Headline numbers for the summary row under the screen header. */
export interface MissionPulse {
  running: number;
  waiting: number;
  done_today: number;
  oldest_wait_s: number;
  generated_at: number;
}

export interface MissionQueue {
  cards: MissionCard[];
  pulse: MissionPulse;
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
  /** Fixed label the server shows for a single-model assistant. */
  model?: string;
  /** Selectable in-chat models (#308/#361), default first. Empty ⇒ no picker
   *  for this assistant (mirrors the web `assistantModels`). */
  models?: string[];
  /** Zero-cost provider (e.g. opencode-zen, #395) — drives the "free" marker. */
  free?: boolean;
  /** Provider may train on submitted data (Zen free models) — drives the
   *  data-training disclosure note. */
  trainingDisclosure?: boolean;
}

export interface HypervisorConfig {
  enabled: boolean;
  defaultAssistant: string;
  workdir: string;
  readOnly: boolean;
  assistants: HypervisorAssistant[];
  /** Whether POST /api/hypervisor/transcribe has an STT provider key (issue
   *  #396) — gates the composer mic on clients without SpeechRecognition. */
  stt?: boolean;
}

export interface HypervisorThread {
  id: string;
  title: string;
  assistant: string | null;
  /** Model this thread runs on (#308), persisted server-side in the thread's
   *  meta. Drives the mobile switcher's current value when the thread reopens. */
  model?: string;
  status: string;
  created_at: number | null;
  updated_at: number | null;
  // AI CTO (#465): 'cto' for a CTO thread, '' for a plain chat; project_id when
  // bound. Lets the CTO screen filter its thread list.
  persona?: string;
  project_id?: string;
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

// ---- Messaging gateway config (issues #328/#329/#330) ----------------------
// Data-driven provider catalog + redacted per-workspace credential store, shared
// with the web dashboard (charts/workspace/web/src/api/gateway.ts).
export interface GatewayCredentialField {
  key: string;
  label: string;
  secret: boolean;
  placeholder: string;
  help_url: string;
  required: boolean;
}

export interface GatewayProviderSpec {
  id: string;
  display_name: string;
  credential_fields: GatewayCredentialField[];
  sender_field: GatewayCredentialField;
  capabilities: { proactive: boolean; max_text_len: number; [k: string]: unknown };
}

export interface GatewayCredentialFieldState {
  set: boolean;
  hint?: string; // secret fields only, e.g. "…9999"; never the value
  value?: string; // non-secret fields only
}

export interface GatewayCredentialsView {
  configured: boolean;
  provider_id: string | null;
  sender_field?: string;
  fields: Record<string, GatewayCredentialFieldState>;
}

export interface GatewayLinkBinding {
  workspace: string;
  workspace_host: string;
  is_default: boolean;
  has_thread: boolean;
  token_set: boolean;
  bound_at: number;
}

export interface GatewayLink {
  id: string;
  channel: string;
  created_at: number;
  updated_at: number;
  bindings: GatewayLinkBinding[];
}

export interface GatewayPairingCode {
  code: string;
  expires_in: number;
  whatsapp_number: string;
  workspace: string;
}

// ---- AI CTO / Projects (#464-#467) -----------------------------------------
// Mirrors charts/workspace/web/src/api/projects.ts. A project is a thin
// registry record the server aggregates on read; the CTO chat rides the
// hypervisor with persona=cto + project_id.

export interface ProjectPulse {
  running: number;
  waiting: number;
  last_activity_at: number | null;
}

export interface Project {
  id: string;
  name: string;
  workdirs: string[];
  repo: string;
  memory_namespace: string;
  status: 'active' | 'paused' | 'archived';
  north_star: string;
  last_seen_at: number | null;
  created_at: number;
  updated_at: number;
  pulse?: ProjectPulse;
}

export interface BriefMemory {
  namespace: string;
  key: string;
  value: string;
  tags: string[];
  importance: number | null;
  updated_at: number | null;
}

export interface BriefTaskRef {
  task_id: string;
  status: string;
  prompt: string;
  workdir: string;
  assistant?: string;
  last_activity_at: number | null;
}

export interface ProjectBrief {
  project: Project;
  tasks: { running: number; waiting: number; total: number; recent: BriefTaskRef[] };
  goals: BriefMemory[];
  decisions: BriefMemory[];
  memories: BriefMemory[];
  git: { workdir: string; branch: string; exists: boolean }[];
  triggers: { kind: string; id: string; workdir?: string; schedule?: string }[];
  counts: { goals: number; decisions: number; memories: number; tasks: number };
  brief_markdown: string;
}

// ---- Feed (#469/#470) ------------------------------------------------------
// Mirrors charts/workspace/web/src/api/feed.ts. One reverse-chronological
// stream of briefings, news, activity, and decisions.

export type FeedKind = 'briefing' | 'news' | 'activity' | 'decision';

export interface FeedLink {
  label: string;
  /** Typed internal ref: task:<id> | thread:<id> | memory:<ns>/<key>. */
  ref?: string;
  /** External URL. */
  href?: string;
}

export interface FeedItem {
  id: string;
  ts: number;
  kind: FeedKind;
  title: string;
  body_md: string;
  source: string;
  project_id: string;
  links: FeedLink[];
  waiting: boolean;
  read: boolean;
}

// ---- Triggers: webhooks + crons (#250) -------------------------------------
// Mirrors charts/workspace/web/src/api/triggers.ts and the backend's
// WebhookManager/CronManager public views (server.py). Secret material is never
// returned by the list endpoints — only `*_set` booleans — except once at
// create time (`hmac_secret_once` / `fire_token_once`).

export type TriggerKind = 'webhook' | 'cron';

export interface WebhookRecord {
  id: string;
  prompt_template: string;
  workdir?: string;
  interpolate_mode?: 'attach' | 'interpolate';
  provider?: string;
  signature_header?: string;
  signature_algo?: string;
  created_at?: number;
  /** True when an HMAC secret is configured. */
  hmac_secret_set?: boolean;
  /** True when NO secret is configured — the server then fails closed. */
  unsigned?: boolean;
  /** Returned once, on create, so the secret can be copied to the sender. */
  hmac_secret_once?: string;
  receive_url?: string;
}

export interface CronRecord {
  id: string;
  schedule: string;
  prompt_template: string;
  workdir?: string;
  timezone?: string;
  suspended?: boolean;
  created_at?: number;
  fire_token_set?: boolean;
  /** kubectl-derived status, decorated by the server (best-effort). */
  last_schedule_time?: string;
  active?: number;
}

/** Webhooks and crons folded into one list, the way both UIs present them. */
export interface Trigger {
  kind: TriggerKind;
  id: string;
  prompt: string;
  workdir?: string;
  created_at?: number;
  /** cron only */
  schedule?: string;
  timezone?: string;
  suspended?: boolean;
  /** webhook only */
  unsigned?: boolean;
  receive_url?: string;
}

export interface CreateWebhookInput {
  id: string;
  prompt_template: string;
  workdir?: string;
}

export interface CreateCronInput {
  id: string;
  schedule: string;
  prompt_template: string;
  workdir?: string;
  timezone?: string;
}

// ---- Docs (in-app documentation site, #250) --------------------------------
// Mirrors charts/workspace/web/src/api/docs.ts and the server's DocsManager:
// a manifest of sections → pages, and markdown fetched per page.

export interface DocsPageMeta {
  id: string;
  title: string;
  file: string;
  summary?: string;
}

export interface DocsSection {
  id: string;
  title: string;
  pages: DocsPageMeta[];
}

export interface DocsManifest {
  version: number;
  sections: DocsSection[];
}

export interface DocsPage {
  id: string;
  title: string;
  summary?: string;
  section_id: string;
  section_title: string;
  file: string;
  /** mtime of the source markdown file, epoch seconds. */
  edited_at: number;
  markdown: string;
}

// ---- Board Processor review (#588 Phase 6) ---------------------------------

/** A write an agent WANTS to make, held until a human approves it. */
export interface BoardStagedAction {
  id: string;
  action: string;
  params: Record<string, unknown>;
  /** What the reviewer reads. Rendered as plain text, never as markup. */
  preview: string;
  writes: number;
  state: 'pending' | 'done' | 'failed' | 'discarded';
  edited?: boolean;
}

/** One item awaiting a decision, with everything needed to decide it on one
 *  screen — proposed writes, reason, evidence, deep link. If a decision needs
 *  the agent's full log, the agent did not summarise well enough. */
export interface BoardReviewItem {
  board_id: string;
  item_id: string;
  item_key: string;
  item_title: string;
  item_url: string;
  /** Echoed back on approve: an approver holding a stale card cannot approve
   *  the newer thing that replaced it. */
  content_hash: string;
  state: 'pending' | 'approved' | 'rejected' | 'sent_back' | 'partial';
  disposition: string | null;
  reason: string;
  evidence: Record<string, unknown>;
  actions: BoardStagedAction[];
  pending_actions: BoardStagedAction[];
  open: boolean;
  decided_by: string;
  created_at: number;
  updated_at: number;
}

export interface BoardReviewGroup {
  disposition: string;
  count: number;
  items: BoardReviewItem[];
}

export interface BoardSummary {
  id: string;
  display_name: string;
  vendor: string;
  credential_set?: boolean;
}
