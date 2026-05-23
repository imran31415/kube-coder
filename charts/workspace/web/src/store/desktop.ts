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
      pushToast(`Launched build · ${r.task_id}`, { kind: 'success' });
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
