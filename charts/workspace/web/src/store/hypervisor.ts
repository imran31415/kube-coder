import { signal } from '@preact/signals';
import {
  getHypervisorConfig,
  listThreads,
  createThread,
  getThread,
  sendThreadMessage,
  deleteThread,
  type HypervisorConfig,
  type HypervisorThread,
} from '../api/hypervisor';
import type { HvEvent } from '../routes/hypervisor/transcript';
import { listTasks, type TaskSummary } from '../api/tasks';

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

export const activeThreadId = signal<string | null>(null);
/** Canonical event stream for the open thread (user turns, assistant prose,
 *  tool calls/results, errors). Rendered by buildTurns() in transcript.ts. */
export const events = signal<HvEvent[]>([]);
export const activeStatus = signal<string>('');

export const sending = signal(false);
export const chatError = signal<string | null>(null);

/** The assistant a NEW thread will use (defaults to config.defaultAssistant). */
export const selectedAssistant = signal<string>('');

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
  } catch (e) {
    configError.value = e instanceof Error ? e.message : 'Failed to load config';
  }
  await refreshThreads();
}

export async function refreshThreads(): Promise<void> {
  threadsLoading.value = true;
  try {
    threads.value = await listThreads();
  } catch {
    /* keep last-good list */
  } finally {
    threadsLoading.value = false;
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

async function pollActive(): Promise<void> {
  const id = activeThreadId.value;
  if (!id) return;
  try {
    // Re-fetch the full (small) transcript each tick — simplest correct model
    // for a chat; the event log is append-only so this never flickers.
    const detail = await getThread(id, 0);
    // Guard against a late poll landing after the user switched threads.
    if (activeThreadId.value !== id) return;
    events.value = detail.events;
    activeStatus.value = detail.thread.status;
  } catch {
    /* transient — next tick retries */
  }
}

export async function openThread(id: string): Promise<void> {
  activeThreadId.value = id;
  events.value = [];
  activeStatus.value = '';
  chatError.value = null;
  await pollActive();
  startPolling();
}

export function closeThread(): void {
  stopPolling();
  activeThreadId.value = null;
  events.value = [];
  activeStatus.value = '';
}

let optimisticSeq = -1;

/** Send a chat message. Creates a new thread if none is active. */
export async function sendMessage(text: string): Promise<void> {
  const trimmed = text.trim();
  if (!trimmed || sending.value) return;
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
      });
      await refreshThreads();
      await openThread(thread.id);
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

/** Start a brand-new (empty) chat: just clears the active thread so the next
 *  message spawns a fresh session. */
export function newChat(): void {
  closeThread();
}

export async function removeThread(id: string): Promise<void> {
  try {
    await deleteThread(id);
  } catch {
    /* best effort */
  }
  if (activeThreadId.value === id) closeThread();
  await refreshThreads();
}
