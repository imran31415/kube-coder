import { signal, computed } from '@preact/signals';
import {
  listTriggers,
  fireCron,
  suspendCron,
  resumeCron,
  deleteCron,
  testWebhook,
  deleteWebhook,
  type Trigger,
} from '../api/triggers';
import { pushToast } from './ui';

export const triggers = signal<Trigger[]>([]);
export const triggersLoading = signal(false);
export const triggersError = signal<string | null>(null);
export const triggerFilter = signal('');

export const filteredTriggers = computed(() => {
  const needle = triggerFilter.value.trim().toLowerCase();
  if (!needle) return triggers.value;
  return triggers.value.filter((t) =>
    `${t.kind} ${t.id} ${t.prompt} ${t.schedule ?? ''} ${t.workdir ?? ''}`.toLowerCase().includes(needle),
  );
});

export async function refreshTriggers(): Promise<void> {
  triggersLoading.value = true;
  try {
    triggers.value = await listTriggers();
    triggersError.value = null;
  } catch (err) {
    triggersError.value = err instanceof Error ? err.message : String(err);
  } finally {
    triggersLoading.value = false;
  }
}

export async function fire(t: Trigger): Promise<void> {
  try {
    if (t.kind === 'cron') await fireCron(t.id);
    else await testWebhook(t.id);
    pushToast(`Fired ${t.id}`, { kind: 'success' });
    await refreshTriggers();
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Fire failed', { kind: 'danger' });
  }
}

export async function toggleSuspend(t: Trigger): Promise<void> {
  if (t.kind !== 'cron') return;
  try {
    if (t.suspended) await resumeCron(t.id);
    else await suspendCron(t.id);
    pushToast(t.suspended ? 'Resumed' : 'Paused', { kind: 'info' });
    await refreshTriggers();
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Toggle failed', { kind: 'danger' });
  }
}

export async function removeTrigger(t: Trigger): Promise<void> {
  try {
    if (t.kind === 'cron') await deleteCron(t.id);
    else await deleteWebhook(t.id);
    pushToast(`Deleted ${t.id}`, { kind: 'warn' });
    await refreshTriggers();
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Delete failed', { kind: 'danger' });
  }
}

let pollHandle: ReturnType<typeof setInterval> | null = null;
export function startTriggerPolling(intervalMs = 30000) {
  void refreshTriggers();
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(() => void refreshTriggers(), intervalMs);
}
export function stopTriggerPolling() {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
}
