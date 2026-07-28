import { signal } from '@preact/signals';
import {
  getHypervisorConfig,
  listThreads,
  createThread,
  getThread,
  sendThreadMessage,
  stopThread,
  deleteThread,
  listDeletedThreads,
  restoreThread,
  renameThread,
  setThreadModel,
  setThreadEffort,
  type HypervisorConfig,
  type HypervisorThread,
  type TranscriptSource,
} from '../api/hypervisor';
import type { HvEvent } from '../routes/hypervisor/transcript';
import { listTasks, type TaskSummary } from '../api/tasks';
import { navigate, currentPath } from './router';
import { rememberLastSession, forgetLastSession } from './lastSession';
import { claudeReady } from './claude';

/**
 * State for the Hypervisor chat tab. A thread is a structured agent session; the
 * store polls its canonical event stream while open and renders those events
 * directly. There is no bespoke LLM loop here — the selected CLI agent does the
 * thinking + tool calls; we normalize its structured output into events.
 */

export const config = signal<HypervisorConfig | null>(null);
export const configError = signal<string | null>(null);

export const threads = signal<HypervisorThread[]>([]);
export const threadsLoading = signal(false);

/**
 * Chat "surface" context (#466). Lets the AI CTO page reuse this store + the
 * `<Chat/>` component without forking: when a route sets persona='cto' + a
 * project, new threads are created bound to that persona/project and the thread
 * list is filtered to it. The default ('' / null) is the plain Hypervisor tab —
 * unchanged, and its list excludes CTO threads so the two surfaces don't mix.
 * The CTO route sets this on mount and resets it on unmount.
 */
export const chatPersona = signal<string>('');
export const chatProjectId = signal<string | null>(null);

export function setChatContext(persona: string, projectId: string | null): void {
  chatPersona.value = persona;
  chatProjectId.value = projectId;
}

/** Soft-deleted threads, shown in the "Recently deleted" section so an
 *  accidental delete can be restored. Loaded lazily when the user expands it. */
export const deletedThreads = signal<HypervisorThread[]>([]);
export const deletedLoading = signal(false);

export const activeThreadId = signal<string | null>(null);
/** Canonical event stream for the open thread (user turns, assistant prose,
 *  tool calls/results, errors). Rendered by buildTurns() in transcript.ts. */
export const events = signal<HvEvent[]>([]);
export const activeStatus = signal<string>('');
/** Where the rendered transcript is sourced from — 'session_log' (Claude Code's
 *  own JSONL log) or 'capture' (the live stream fallback). Drives a small chip
 *  in the chat so it's clear the transcript is the structured, durable one. */
export const transcriptSource = signal<TranscriptSource | null>(null);

export const sending = signal(false);
/** True from the moment the user hits Stop until the turn actually ends, so the
 *  Stop button can show a pending state and not be double-fired. */
export const stopping = signal(false);
export const chatError = signal<string | null>(null);

/** The assistant a NEW thread will use (defaults to config.defaultAssistant). */
export const selectedAssistant = signal<string>('');

/** The model a NEW thread will use (#308) — the selected assistant's default
 *  (first entry of its `models`) unless the user picks another. '' when the
 *  assistant offers no model choice. For an already-open thread the switcher
 *  acts on that thread instead (setActiveThreadModel). */
export const selectedModel = signal<string>('');

/** The reasoning effort a NEW thread will use (#362) — the selected assistant's
 *  default (its config `effort`) unless the user picks another. '' when the
 *  assistant offers no effort choice. For an already-open thread the selector
 *  acts on that thread instead (setActiveThreadEffort). */
export const selectedEffort = signal<string>('');

/**
 * The AI CTO's OWN assistant/model/effort (#483). Deliberately separate from
 * the three signals above: the CTO page reuses this store, and before this it
 * inherited whatever the Chat tab happened to have selected — so a model picked
 * for a throwaway chat silently became the CTO's. These are seeded from the
 * selected project's defaults (falling back to the workspace default) and are
 * what sendMessage passes when the surface is the CTO.
 */
export const ctoAssistant = signal<string>('');
export const ctoModel = signal<string>('');
export const ctoEffort = signal<string>('');

/** The folder a NEW thread starts in (#345) — seeded from config.workdir (the
 *  server's HYPERVISOR_WORKDIR) so the picker shows the real default. '' is
 *  passed as no workdir, letting the server default apply. A thread keeps the
 *  folder it was created in for life; this only affects the next new chat. */
export const selectedWorkdir = signal<string>('');

/** Selectable models for an assistant id, from the loaded config (default
 *  first). Empty when the assistant offers no in-chat model choice. */
export function assistantModels(assistantId: string | null | undefined): string[] {
  if (!assistantId) return [];
  const a = (config.value?.assistants ?? []).find((x) => x.id === assistantId);
  return a?.models ?? [];
}

/** The full config entry for an assistant id (or undefined). */
export function assistantInfo(assistantId: string | null | undefined) {
  if (!assistantId) return undefined;
  return (config.value?.assistants ?? []).find((x) => x.id === assistantId);
}

/** Selectable reasoning-effort levels for an assistant (#362), or [] when the
 *  assistant has no effort knob (the selector stays hidden). */
export function assistantEfforts(assistantId: string | null | undefined): string[] {
  return assistantInfo(assistantId)?.efforts ?? [];
}

/** The assistant's default effort level ('' when it has no knob). */
export function assistantEffortDefault(assistantId: string | null | undefined): string {
  return assistantInfo(assistantId)?.effort ?? '';
}

/** The highest level the assistant honours natively ('' when unbounded/none) —
 *  a pick above it is clamped, so the UI shows a "runs <cap>" hint. */
export function assistantEffortCap(assistantId: string | null | undefined): string {
  return assistantInfo(assistantId)?.effortCap ?? '';
}

/** True when the assistant is a free provider that may train on submitted data
 *  (Zen free models, #395) — drives the in-chat disclosure note. */
export function assistantNeedsDisclosure(assistantId: string | null | undefined): boolean {
  return !!assistantInfo(assistantId)?.trainingDisclosure;
}

/** Pick the assistant a new chat will use, resetting the model to that
 *  assistant's default so the switcher never shows an off-list model. */
export function setSelectedAssistant(assistantId: string): void {
  selectedAssistant.value = assistantId;
  selectedModel.value = assistantModels(assistantId)[0] ?? '';
  // Reset effort to the new assistant's default (#362) so the selector never
  // shows a level the assistant doesn't offer.
  selectedEffort.value = assistantEffortDefault(assistantId);
}

/** Same, for the CTO surface's own selection (#483). */
export function setCtoAssistant(assistantId: string): void {
  ctoAssistant.value = assistantId;
  ctoModel.value = assistantModels(assistantId)[0] ?? '';
  ctoEffort.value = assistantEffortDefault(assistantId);
}

/**
 * Seed the CTO's selection from a project's stored defaults (#483), falling back
 * to the workspace default when the project has none — which is the state every
 * project starts in, so nothing is written until the user explicitly sets one.
 * A stored value the workspace can no longer offer (a provider whose key is
 * gone, a model dropped from the curated list) falls back too, so the picker
 * never shows a dead option. Called whenever the selected project changes.
 */
export function seedCtoConfig(
  project: {
    default_assistant?: string;
    default_model?: string;
    default_effort?: string;
  } | null,
): void {
  const wantAssistant = project?.default_assistant || '';
  const known = (config.value?.assistants ?? []).some((a) => a.id === wantAssistant);
  const assistant =
    (known ? wantAssistant : '') || config.value?.defaultAssistant || 'claude';
  ctoAssistant.value = assistant;
  const models = assistantModels(assistant);
  const wantModel = project?.default_model || '';
  ctoModel.value =
    (wantModel && models.includes(wantModel) ? wantModel : models[0]) ?? '';
  const levels = assistantEfforts(assistant);
  const wantEffort = project?.default_effort || '';
  ctoEffort.value =
    wantEffort && levels.includes(wantEffort)
      ? wantEffort
      : assistantEffortDefault(assistant);
}

/** True when the current chat surface is the AI CTO rather than the Chat tab. */
export function isCtoSurface(): boolean {
  return chatPersona.value === 'cto';
}

/** The assistant/model/effort a NEW thread on the CURRENT surface will use.
 *  Reading through these (rather than the raw signals) is what keeps the CTO
 *  and the Chat tab from borrowing each other's selection (#483). */
export function surfaceAssistant(): string {
  return isCtoSurface() ? ctoAssistant.value : selectedAssistant.value;
}

export function surfaceModel(): string {
  return isCtoSurface() ? ctoModel.value : selectedModel.value;
}

export function surfaceEffort(): string {
  return isCtoSurface() ? ctoEffort.value : selectedEffort.value;
}

/** Live workspace "entities" surfaced as chips in the chat — currently the
 *  other tasks/agents running in the pod, so the user can see what the
 *  Hypervisor is talking about without leaving the chat. */
export const workspaceTasks = signal<TaskSummary[]>([]);

export async function refreshWorkspaceTasks(): Promise<void> {
  try {
    workspaceTasks.value = await listTasks();
  } catch {
    /* keep last-good list */
  }
}

let pollTimer: number | null = null;

export async function initHypervisor(): Promise<void> {
  configError.value = null;
  try {
    const cfg = await getHypervisorConfig();
    config.value = cfg;
    if (!selectedAssistant.value) {
      selectedAssistant.value = cfg.defaultAssistant || 'claude';
    }
    // Seed the model to the selected assistant's default now that the config
    // (and its per-assistant model lists) is loaded (#308).
    if (!selectedModel.value) {
      selectedModel.value = assistantModels(selectedAssistant.value)[0] ?? '';
    }
    // Seed the effort to the selected assistant's default now that the config
    // (and its per-assistant effort lists) is loaded (#362).
    if (!selectedEffort.value) {
      selectedEffort.value = assistantEffortDefault(selectedAssistant.value);
    }
    // The CTO surface reseeds from its project (seedCtoConfig) as soon as one is
    // selected; this only covers the gap before that lands (#483).
    if (!ctoAssistant.value) seedCtoConfig(null);
    if (!selectedWorkdir.value) {
      selectedWorkdir.value = cfg.workdir || '';
    }
  } catch (e) {
    configError.value = e instanceof Error ? e.message : 'Failed to load config';
  }
  await refreshThreads();
}

export async function refreshThreads(): Promise<void> {
  threadsLoading.value = true;
  try {
    // Scope the list to the current surface: the CTO page sees only its
    // persona (+ project); the plain Chat tab excludes CTO threads (#465/#466).
    const filter =
      chatPersona.value === 'cto'
        ? { persona: 'cto' as const, project: chatProjectId.value ?? undefined }
        : { persona: 'default' as const };
    threads.value = await listThreads(filter);
  } catch {
    /* keep last-good list */
  } finally {
    threadsLoading.value = false;
  }
}

export async function refreshDeletedThreads(): Promise<void> {
  deletedLoading.value = true;
  try {
    deletedThreads.value = await listDeletedThreads();
  } catch {
    /* keep last-good list */
  } finally {
    deletedLoading.value = false;
  }
}

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function startPolling(): void {
  stopPolling();
  if (typeof window === 'undefined') return;
  pollTimer = window.setInterval(pollActive, 2000);
}

/** True when a freshly polled transcript is content-identical to the one we
 *  already hold. Each poll re-fetches the full transcript, so `detail.events`
 *  is a new array identity every tick even when nothing changed — assigning it
 *  unconditionally re-fired every `events` subscriber (notably the chat's
 *  scroll-pin effect) every 2s on an idle thread (#348). Events are
 *  append-only and immutable per seq within a source, so length + last-event
 *  equality is a sufficient content proxy; a source flip (capture ↔
 *  session_log) re-stamps seqs, so it always counts as changed. */
export function sameTranscript(
  prev: HvEvent[],
  next: HvEvent[],
  prevSource: TranscriptSource | null,
  nextSource: TranscriptSource | null,
): boolean {
  if (prevSource !== nextSource || prev.length !== next.length) return false;
  if (next.length === 0) return true;
  const a = prev[prev.length - 1];
  const b = next[next.length - 1];
  return a.seq === b.seq && a.type === b.type && a.text === b.text;
}

async function pollActive(): Promise<void> {
  const id = activeThreadId.value;
  if (!id) return;
  try {
    // Re-fetch the full (small) transcript each tick — simplest correct model
    // for a chat.
    const detail = await getThread(id, 0);
    // Guard against a late poll landing after the user switched threads.
    if (activeThreadId.value !== id) return;
    // Only swap `events` when content actually changed, keeping its identity
    // stable across idle ticks — see sameTranscript (#348).
    const source = detail.source ?? null;
    if (!sameTranscript(events.value, detail.events, transcriptSource.value, source)) {
      events.value = detail.events;
    }
    activeStatus.value = detail.thread.status;
    transcriptSource.value = source;
  } catch {
    /* transient — next tick retries */
  }
}

export async function openThread(id: string): Promise<void> {
  activeThreadId.value = id;
  // Remember the last-open chat so a later bare visit (returning to the app,
  // clicking the tab) can reopen it — see store/lastSession.ts. Keyed by
  // surface so the CTO page and the Chat tab restore independently (#466).
  rememberLastSession(chatPersona.value === 'cto' ? 'cto' : 'hypervisor', id);
  events.value = [];
  activeStatus.value = '';
  transcriptSource.value = null;
  chatError.value = null;
  await pollActive();
  startPolling();
}

export function closeThread(): void {
  stopPolling();
  activeThreadId.value = null;
  events.value = [];
  activeStatus.value = '';
  transcriptSource.value = null;
}

let optimisticSeq = -1;

/** Send a chat message. Creates a new thread if none is active. */
export async function sendMessage(text: string): Promise<void> {
  const trimmed = text.trim();
  if (!trimmed || sending.value) return;
  // Gate the AI CTO first-win path on a working Claude credential (#494): with
  // none present, firing a task just dies with a raw provider error, so refuse
  // the send and point the user at the connect panel the CTO welcome renders.
  // Only blocks the CTO surface, and only when readiness is known-false (null =
  // not yet probed → don't block an existing authenticated user).
  if (chatPersona.value === 'cto' && claudeReady.value === false) {
    chatError.value =
      'Connect your Claude account to start building — use the connect options above.';
    return;
  }
  sending.value = true;
  chatError.value = null;
  // Optimistically show the user's turn until the next poll replaces it with
  // the server-recorded event (negative seq so it never collides).
  events.value = [
    ...events.value,
    { seq: optimisticSeq--, ts: Date.now() / 1000, role: 'user', type: 'message', text: trimmed },
  ];
  activeStatus.value = 'running';
  try {
    if (!activeThreadId.value) {
      const thread = await createThread({
        message: trimmed,
        // Surface-scoped (#483): the CTO uses its own project-seeded selection,
        // the Chat tab its own — neither borrows the other's.
        assistant: surfaceAssistant() || undefined,
        model: surfaceModel() || undefined,
        effort: surfaceEffort() || undefined,
        // A CTO thread omits the workdir so the server defaults it to the bound
        // project's first workdir; a plain chat sends the picker's folder. Sending
        // the picker's /home/dev default would defeat the server's project
        // default (its `not workdir` guard would never fire).
        workdir:
          chatPersona.value === 'cto'
            ? undefined
            : selectedWorkdir.value || undefined,
        // AI CTO (#465): bind new threads to the CTO persona + project when the
        // CTO page set that context; undefined (a plain chat) otherwise.
        persona: chatPersona.value || undefined,
        project_id: chatProjectId.value || undefined,
      });
      await refreshThreads();
      await openThread(thread.id);
      // Reflect the new thread in the URL so a refresh reopens it. Guarded so
      // we only touch history when actually on the Hypervisor route — the CTO
      // page keeps its thread in the store + localStorage, not the URL.
      if (currentPath.value.startsWith('/hypervisor')) {
        navigate(`/hypervisor/${encodeURIComponent(thread.id)}`, true);
      }
    } else {
      await sendThreadMessage(activeThreadId.value, trimmed);
      startPolling();
      await pollActive();
    }
  } catch (e) {
    chatError.value = e instanceof Error ? e.message : 'Failed to send';
  } finally {
    sending.value = false;
  }
}

/** Stop the turn currently running in the active thread. Best-effort: the
 *  server kills the CLI process and appends a "stopped" marker, which the next
 *  poll surfaces; we also refresh immediately so the UI reacts without waiting
 *  for the 2s tick. */
export async function stopMessage(): Promise<void> {
  const id = activeThreadId.value;
  if (!id || stopping.value) return;
  stopping.value = true;
  try {
    await stopThread(id);
    await pollActive();
  } catch (e) {
    chatError.value = e instanceof Error ? e.message : 'Failed to stop';
  } finally {
    stopping.value = false;
  }
}

/** Start a brand-new (empty) chat: just clears the active thread so the next
 *  message spawns a fresh session. */
export function newChat(): void {
  closeThread();
}

/** Rename a chat. Optimistically patches the in-memory list so the sidebar and
 *  topbar update instantly, then confirms against the server. */
export async function renameThreadTitle(id: string, title: string): Promise<void> {
  const trimmed = title.trim();
  if (!trimmed) return;
  const prev = threads.value;
  threads.value = prev.map((t) => (t.id === id ? { ...t, title: trimmed } : t));
  try {
    await renameThread(id, trimmed);
    await refreshThreads();
  } catch {
    // Roll back to the last-good list on failure.
    threads.value = prev;
  }
}

/** Switch the model (#308). With a thread open, updates that thread server-side
 *  (takes effect next turn) and optimistically patches the list so the switcher
 *  reflects it at once; with no thread open, just updates the new-chat default. */
export async function setActiveThreadModel(model: string): Promise<void> {
  const id = activeThreadId.value;
  if (!id) {
    // Move the current surface's new-chat default, not the other's (#483).
    if (isCtoSurface()) ctoModel.value = model;
    else selectedModel.value = model;
    return;
  }
  const prev = threads.value;
  threads.value = prev.map((t) => (t.id === id ? { ...t, model } : t));
  try {
    await setThreadModel(id, model);
    await refreshThreads();
  } catch {
    threads.value = prev;
  }
}

/** Switch the reasoning effort (#362), twin of setActiveThreadModel. With a
 *  thread open, updates it server-side (takes effect next turn) and optimistically
 *  patches the list; with no thread open, just updates the new-chat default. */
export async function setActiveThreadEffort(effort: string): Promise<void> {
  const id = activeThreadId.value;
  if (!id) {
    if (isCtoSurface()) ctoEffort.value = effort;
    else selectedEffort.value = effort;
    return;
  }
  const prev = threads.value;
  threads.value = prev.map((t) => (t.id === id ? { ...t, effort } : t));
  try {
    await setThreadEffort(id, effort);
    await refreshThreads();
  } catch {
    threads.value = prev;
  }
}

/** Soft-delete a chat. The thread moves to "Recently deleted" (restorable)
 *  rather than being erased. Refreshes both lists so the trash view stays
 *  current if it's already been expanded. */
export async function removeThread(id: string): Promise<void> {
  try {
    await deleteThread(id);
  } catch {
    /* best effort */
  }
  if (activeThreadId.value === id) closeThread();
  // Forget the surface's own last-session key (openThread keyed it by persona).
  forgetLastSession(chatPersona.value === 'cto' ? 'cto' : 'hypervisor', id);
  await refreshThreads();
  // Keep the trash view in sync only if it's been loaded at least once.
  if (deletedThreads.value.length > 0) await refreshDeletedThreads();
}

/** Undo a soft-delete: the chat reappears in the main list. */
export async function reviveThread(id: string): Promise<void> {
  try {
    await restoreThread(id);
  } catch {
    /* best effort */
  }
  await Promise.all([refreshThreads(), refreshDeletedThreads()]);
}
