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
  setThreadProject,
  type HypervisorConfig,
  type HypervisorThread,
  type TranscriptSource,
} from '../api/hypervisor';
import type { HvEvent } from '../routes/hypervisor/transcript';
import { listTasks, type TaskSummary } from '../api/tasks';
import { navigate, currentPath } from './router';
import { rememberLastSession, forgetLastSession } from './lastSession';
import { claudeReady } from './claude';
import { isNotReady, readyOr } from '../util/assistants';

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
 * The mode a NEW chat will carry (#683) — '' for a plain workspace chat, 'cto'
 * for an AI CTO one. Exactly the same two-mode behaviour as the Agent / Folder
 * / Project pickers: it sets what the *next* new chat gets, and an already-open
 * thread is unaffected.
 *
 * Creation-time only, deliberately. The preamble that defines a mode is handed
 * to the agent once, via --append-system-prompt on turn 1, so a thread that
 * switched mid-flight would have two identities and no honest way to show it.
 *
 * This replaced the `chatPersona` / `chatProjectId` "surface context" the AI CTO
 * page set on mount: with one chat surface there is no second surface holding a
 * competing selection, so the mode is just another new-chat default.
 */
export const newChatMode = signal<string>('');

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

/** The project a NEW chat is filed into (#358) — '' for none, which stays the
 *  default so a workspace with no projects is unchanged. For an already-open
 *  chat the picker re-files that chat instead (setActiveThreadProject). */
export const selectedProject = signal<string>('');

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

/** Why a NEW chat can't start on the selected assistant (#702) — its
 *  not-ready reason, e.g. a missing API key — or null when it can. An open
 *  thread is never blocked here; the server refuses its send if needed. */
export function newChatBlockedReason(): string | null {
  if (activeThreadId.value) return null;
  const a = assistantInfo(selectedAssistant.value);
  if (!isNotReady(a)) return null;
  return a?.notReadyReason || `${a?.label ?? 'This agent'} isn't set up yet.`;
}

/** Re-read the Hypervisor config, e.g. after a provider key was saved, so a
 *  not-ready assistant flips to ready without a page reload (#702). */
export async function refreshHypervisorConfig(): Promise<void> {
  try {
    config.value = await getHypervisorConfig();
  } catch {
    /* keep last-good config */
  }
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

/** A project's stored assistant configuration (#483/#362) — the three fields on
 *  the project record, absent/'' meaning "inherit the workspace default". */
export interface ProjectDefaults {
  default_assistant?: string;
  default_model?: string;
  default_effort?: string;
}

/**
 * Resolve a project's stored defaults into a selection this workspace can
 * actually offer (#483), falling back to the workspace default when the project
 * has none — which is the state every project starts in, so nothing is written
 * until the user explicitly sets one. A stored value the workspace can no
 * longer offer (a provider whose key is gone, a model dropped from the curated
 * list) falls back too, so a picker never shows a dead option.
 */
export function resolveProjectDefaults(project: ProjectDefaults | null): {
  assistant: string;
  model: string;
  effort: string;
} {
  const wantAssistant = project?.default_assistant || '';
  const list = config.value?.assistants ?? [];
  // Listed-but-not-ready (#702) counts as unavailable for a default.
  const usable = list.some((a) => a.id === wantAssistant && !isNotReady(a));
  const assistant =
    (usable ? wantAssistant : '') ||
    readyOr(list, config.value?.defaultAssistant || 'claude');
  const models = assistantModels(assistant);
  const wantModel = project?.default_model || '';
  const model = (wantModel && models.includes(wantModel) ? wantModel : models[0]) ?? '';
  const levels = assistantEfforts(assistant);
  const wantEffort = project?.default_effort || '';
  const effort =
    wantEffort && levels.includes(wantEffort)
      ? wantEffort
      : assistantEffortDefault(assistant);
  return { assistant, model, effort };
}

/**
 * Seed Chat's new-chat selection from a project (#683/#483) — the pickers a
 * user actually has in front of them. Only meaningful with no chat open: an
 * existing chat carries its own assistant and model, and re-seeding would
 * silently overrule them.
 */
export function seedChatConfig(project: ProjectDefaults | null): void {
  const next = resolveProjectDefaults(project);
  selectedAssistant.value = next.assistant;
  selectedModel.value = next.model;
  selectedEffort.value = next.effort;
}

/** True when the chat being composed is an AI CTO one (#683) — what decides the
 *  persona sent at creation, the project-resolved workdir, and the
 *  Claude-credential gate. */
export function isCtoChat(): boolean {
  return newChatMode.value === 'cto';
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
      // Never pre-select a listed-but-not-ready agent (#702).
      selectedAssistant.value = readyOr(cfg.assistants ?? [], cfg.defaultAssistant || 'claude');
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
    // ONE list (#683). Every thread, whatever its persona; the modes are a
    // badge + a filter chip in the sidebar (routes/hypervisor/threadMode.ts),
    // not two disjoint lists. Chat used to send `persona=default` and the AI
    // CTO page `persona=cto`, which made a CTO thread invisible to every
    // thread-management affordance Chat has — #663, by construction.
    threads.value = await listThreads();
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
  // clicking the tab) can reopen it — see store/lastSession.ts. One surface, so
  // one key (#683); the CTO page's separate 'cto' key went with the page.
  rememberLastSession('hypervisor', id);
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
  if (isCtoChat() && claudeReady.value === false) {
    chatError.value =
      'Connect your Claude account to start building — use the connect options above.';
    return;
  }
  // A new chat on an agent that can't run yet (#702) would only fail on its
  // first turn — refuse with the reason the picker already shows.
  const blocked = newChatBlockedReason();
  if (blocked) {
    chatError.value = blocked;
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
        assistant: selectedAssistant.value || undefined,
        model: selectedModel.value || undefined,
        effort: selectedEffort.value || undefined,
        // A CTO thread omits the workdir so the server defaults it to the bound
        // project's first workdir; a plain chat sends the picker's folder. Sending
        // the picker's /home/dev default would defeat the server's project
        // default (its `not workdir` guard would never fire).
        // A CTO chat omits the workdir so the server defaults it to the bound
        // project's first workdir; a plain chat sends the picker's folder.
        // Sending the picker's /home/dev default would defeat the server's
        // project default (its `not workdir` guard would never fire).
        workdir: isCtoChat() ? undefined : selectedWorkdir.value || undefined,
        // AI CTO (#465): bind new threads to the CTO persona when the Mode
        // picker says so (#683); undefined (a plain chat) otherwise.
        persona: newChatMode.value || undefined,
        // The project this chat is filed into (#358) — undefined when none.
        project_id: selectedProject.value || undefined,
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
    selectedModel.value = model;
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
    selectedEffort.value = effort;
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

/**
 * File a chat into a project (#358), twin of setActiveThreadModel. With a chat
 * open it re-files that chat server-side (the binding takes effect on its next
 * turn) and optimistically patches the list so the sidebar regroups at once;
 * with no chat open it just moves the new-chat default for this surface.
 * '' clears the binding.
 */
export async function setActiveThreadProject(projectId: string): Promise<void> {
  const id = activeThreadId.value;
  if (!id) {
    selectedProject.value = projectId;
    return;
  }
  const prev = threads.value;
  threads.value = prev.map((t) => (t.id === id ? { ...t, project_id: projectId } : t));
  try {
    await setThreadProject(id, projectId);
    await refreshThreads();
  } catch (e) {
    threads.value = prev;
    chatError.value = e instanceof Error ? e.message : 'Failed to set project';
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
  forgetLastSession('hypervisor', id);
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
