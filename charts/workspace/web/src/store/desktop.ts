import { signal } from '@preact/signals';
import {
  type DesktopItem,
  type DesktopItemDraft,
  listDesktop,
  createDesktopItem,
  updateDesktopItem,
  deleteDesktopItem,
  reorderDesktop,
  launchDesktopItem,
} from '../api/desktop';
import { pushToast } from './ui';
import { navigate } from './router';
import { createTask } from './tasks';
import { createThread, getHypervisorConfig } from '../api/hypervisor';
import { config as hypervisorConfig, refreshThreads } from './hypervisor';

export const desktopItems = signal<DesktopItem[]>([]);
export const desktopError = signal<string | null>(null);
export const desktopLoaded = signal<boolean>(false);

let _inflight: Promise<void> | null = null;
export async function refreshDesktop(): Promise<void> {
  if (_inflight) return _inflight;
  _inflight = (async () => {
    try {
      const r = await listDesktop();
      desktopItems.value = r.items ?? [];
      desktopError.value = null;
    } catch (e) {
      desktopError.value = e instanceof Error ? e.message : String(e);
    } finally {
      desktopLoaded.value = true;
      _inflight = null;
    }
  })();
  return _inflight;
}

export async function saveDesktopItem(
  draft: DesktopItemDraft,
  existingId: string | null,
): Promise<DesktopItem | null> {
  try {
    const item = existingId
      ? await updateDesktopItem(existingId, draft)
      : await createDesktopItem(draft);
    await refreshDesktop();
    return item;
  } catch (e) {
    pushToast(e instanceof Error ? e.message : 'Save failed', { kind: 'danger' });
    return null;
  }
}

export async function removeDesktopItem(id: string): Promise<void> {
  try {
    await deleteDesktopItem(id);
    await refreshDesktop();
  } catch (e) {
    pushToast(e instanceof Error ? e.message : 'Delete failed', { kind: 'danger' });
  }
}

export async function moveDesktopItem(id: string, dir: 'up' | 'down'): Promise<void> {
  const items = desktopItems.value;
  const idx = items.findIndex((it) => it.id === id);
  if (idx < 0) return;
  const target = dir === 'up' ? idx - 1 : idx + 1;
  if (target < 0 || target >= items.length) return;
  const next = items.slice();
  [next[idx], next[target]] = [next[target], next[idx]];
  // Optimistic UI: swap in place, then persist. Re-sync from server in
  // case the server reshuffled for any reason (defensive idempotency).
  desktopItems.value = next;
  try {
    await reorderDesktop(next.map((it) => it.id));
  } catch (e) {
    pushToast(e instanceof Error ? e.message : 'Reorder failed', { kind: 'danger' });
    await refreshDesktop();
  }
}

/** Start a build directly from a free-text prompt typed into the Desktop
 *  composer. Mirrors launchItem's `task` branch: create the task, then drop
 *  the user straight into the new build's detail/terminal view. Returns true
 *  on success so the caller can clear its input. */
export async function startBuildFromPrompt(
  prompt: string,
  workdir: string,
  assistant?: string,
): Promise<boolean> {
  const text = prompt.trim();
  if (!text) return false;
  const task = await createTask({
    prompt: text,
    workdir,
    assistant: assistant || undefined,
  });
  if (!task || !task.task_id) return false;
  // Same landing as launchItem: /tasks/<id> selects the build and mounts the
  // TerminalPane (URL-driven, so it works on both desktop split-view and the
  // mobile full-screen detail route).
  navigate(`/tasks/${encodeURIComponent(task.task_id)}`);
  return true;
}

/** Start a Hypervisor chat directly from the Desktop composer. Mirrors
 *  startBuildFromPrompt but creates a structured chat thread instead of a
 *  build, then deep-links into it (URL-driven, so the thread opens on both
 *  desktop split-view and the mobile full-screen chat). The chat runs in the
 *  chosen workdir and uses the workspace's default assistant unless one is
 *  passed. Returns true on success so the caller can clear its input. */
export async function startChatFromPrompt(
  prompt: string,
  workdir: string,
  assistant?: string,
): Promise<boolean> {
  const text = prompt.trim();
  if (!text) return false;
  try {
    // Resolve the default assistant lazily — the Desktop route doesn't init the
    // Hypervisor store, so config may not be loaded yet on first use.
    let agent = assistant;
    if (!agent) {
      const cfg = hypervisorConfig.value ?? (await getHypervisorConfig());
      hypervisorConfig.value = cfg;
      agent = cfg.defaultAssistant || undefined;
    }
    const thread = await createThread({ message: text, assistant: agent, workdir });
    if (!thread || !thread.id) return false;
    // Refresh the thread list so the sidebar/activity reflect the new chat,
    // then land the user straight in it.
    await refreshThreads();
    navigate(`/hypervisor/${encodeURIComponent(thread.id)}`);
    return true;
  } catch (e) {
    pushToast(e instanceof Error ? e.message : 'Failed to start chat', { kind: 'danger' });
    return false;
  }
}

export async function launchItem(item: DesktopItem): Promise<void> {
  // URL actions skip the server roundtrip — open immediately to keep the
  // popup blocker happy (synchronous-from-click).
  if (item.action.type === 'url') {
    const { url, target } = item.action;
    if (target === 'self') {
      window.location.href = url;
    } else {
      window.open(url, '_blank', 'noopener,noreferrer');
    }
    return;
  }
  try {
    const r = await launchDesktopItem(item.id);
    if (r.kind === 'task') {
      // Jump straight to the new build's detail view — TasksRoute's
      // URL-driven useEffect calls selectTask(task_id) and the
      // TerminalPane mounts. Without this nav the user would just see
      // a toast and have to manually open the Builds list.
      pushToast(`Launched · ${item.label}`, { kind: 'success', ttl: 2500 });
      navigate(`/tasks/${encodeURIComponent(r.task_id)}`);
    } else if (r.kind === 'shell') {
      const ok = r.exit_code === 0;
      pushToast(
        ok
          ? `${item.label} ran cleanly (exit 0)`
          : `${item.label} exited ${r.exit_code}${r.stderr ? ' — ' + r.stderr.split('\n')[0] : ''}`,
        { kind: ok ? 'success' : 'warn', ttl: ok ? 3000 : 6000 },
      );
    }
  } catch (e) {
    pushToast(e instanceof Error ? e.message : 'Launch failed', { kind: 'danger' });
  }
}
