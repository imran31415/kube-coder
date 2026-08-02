import { signal } from '@preact/signals';
import { subscribeEvents, type DashboardEvent } from '../api/events';
import {
  getDevcontainer,
  applyDevcontainer,
  resetDevcontainer,
  DevcontainerConflictError,
  type DevcontainerRecord,
  type HookName,
  type ApplyResult,
} from '../api/devcontainer';
import { pushToast } from './ui';

/**
 * devcontainer store (#594) — mirrors store/skills.ts: signals, in-flight
 * dedupe, and an SSE subscription instead of polling.
 *
 * Keyed by WORKDIR, not project id: a project has N workdirs and a git
 * worktree carries its own devcontainer.json, so a per-project cache would
 * show the wrong file the moment someone opens a worktree.
 */

export const devcontainers = signal<Record<string, DevcontainerRecord>>({});
export const devcontainerLoading = signal(false);
export const devcontainerError = signal<string | null>(null);
export const applyingDevcontainer = signal(false);

/** The workdir whose apply dialog is open, or null. */
export const applyDialogWorkdir = signal<string | null>(null);

export function devcontainerFor(workdir: string): DevcontainerRecord | null {
  return devcontainers.value[workdir] ?? null;
}

// Dedupe per workdir so an SSE-triggered reload and a mount-time load can't
// race and clobber each other with the slower response.
const inFlight = new Map<string, Promise<void>>();

export async function loadDevcontainer(workdir: string, quiet = false): Promise<void> {
  if (!workdir) return;
  const existing = inFlight.get(workdir);
  if (existing) return existing;
  if (!quiet) devcontainerLoading.value = true;
  const p = (async () => {
    try {
      const rec = await getDevcontainer(workdir);
      devcontainers.value = { ...devcontainers.value, [workdir]: rec };
      devcontainerError.value = null;
    } catch (err) {
      // Keep whatever we last rendered — a transient failure should not blank
      // the panel and make the user think the file vanished.
      devcontainerError.value = err instanceof Error ? err.message : String(err);
    } finally {
      if (!quiet) devcontainerLoading.value = false;
      inFlight.delete(workdir);
    }
  })();
  inFlight.set(workdir, p);
  return p;
}

export function openApplyDialog(workdir: string) {
  applyDialogWorkdir.value = workdir;
}

export function closeApplyDialog() {
  applyDialogWorkdir.value = null;
}

export type ApplyOutcome =
  | { ok: true; result: ApplyResult }
  | { ok: false; conflict: string }
  | { ok: false; error: string };

/**
 * Apply a devcontainer.json. `configHash` MUST be the hash that came back with
 * the record the user was shown — the server refuses a mismatch with 409, and
 * that refusal is the whole consent guarantee.
 */
export async function applyDevcontainerTo(
  workdir: string,
  hooks: HookName[],
  configHash: string,
  autoApply?: boolean,
): Promise<ApplyOutcome> {
  applyingDevcontainer.value = true;
  try {
    const result = await applyDevcontainer({
      workdir,
      hooks,
      config_hash: hooks.length ? configHash : undefined,
      auto_apply: autoApply,
    });
    const bits: string[] = [];
    if (result.ports_pinned.length) bits.push(`${result.ports_pinned.length} ports`);
    if (result.extensions_installed.length) {
      bits.push(`${result.extensions_installed.length} extensions`);
    }
    if (result.settings_written.length) bits.push(`${result.settings_written.length} settings`);
    if (result.hooks_started.length) bits.push(`${result.hooks_started.join(', ')} running`);
    pushToast(bits.length ? `Applied — ${bits.join(', ')}` : 'Nothing to apply',
      { kind: 'success' });
    if (result.port_conflicts.length) {
      pushToast(
        `${result.port_conflicts.length} port(s) already pinned under another name — left alone`,
        { kind: 'warn' },
      );
    }
    await loadDevcontainer(workdir, true);
    return { ok: true, result };
  } catch (err) {
    if (err instanceof DevcontainerConflictError) {
      // Re-load so the dialog can show what the file says NOW rather than
      // letting the user retry against text that is already stale.
      await loadDevcontainer(workdir, true);
      const msg = err.code === 'busy'
        ? 'A devcontainer run is already in progress here.'
        : 'devcontainer.json changed — review the new commands and confirm again.';
      pushToast(msg, { kind: 'warn' });
      return { ok: false, conflict: err.code };
    }
    const msg = err instanceof Error ? err.message : 'Apply failed';
    pushToast(msg, { kind: 'danger' });
    return { ok: false, error: msg };
  } finally {
    applyingDevcontainer.value = false;
  }
}

export async function resetDevcontainerAt(workdir: string, unpinPorts = false): Promise<boolean> {
  try {
    await resetDevcontainer(workdir, unpinPorts);
    pushToast('Devcontainer state cleared', { kind: 'success' });
    await loadDevcontainer(workdir, true);
    return true;
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Reset failed', { kind: 'danger' });
    return false;
  }
}

// Real-time: the backend publishes `devcontainer.changed` when an apply lands
// or a hook changes state, so a ten-minute postCreate reports progress without
// the panel polling for it.
let eventUnsub: (() => void) | null = null;

function onDevcontainerEvent(ev: DashboardEvent) {
  if (ev.type !== 'devcontainer.changed') return;
  const workdir = (ev.data as { workdir?: string } | undefined)?.workdir;
  // Reload ONLY the affected workdir — a burst of hook events must not refetch
  // every devcontainer the user has ever opened.
  if (workdir && devcontainers.value[workdir]) void loadDevcontainer(workdir, true);
}

export function startDevcontainerEvents() {
  if (!eventUnsub) eventUnsub = subscribeEvents(onDevcontainerEvent);
}

export function stopDevcontainerEvents() {
  if (eventUnsub) {
    eventUnsub();
    eventUnsub = null;
  }
}

/** Test-only reset so suites don't leak state into each other. */
export function _resetDevcontainerForTest() {
  devcontainers.value = {};
  devcontainerLoading.value = false;
  devcontainerError.value = null;
  applyingDevcontainer.value = false;
  applyDialogWorkdir.value = null;
  inFlight.clear();
  stopDevcontainerEvents();
}
