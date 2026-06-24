import { signal } from '@preact/signals';
import {
  type Workspace,
  type WorkspaceState,
  listWorkspaces,
  startWorkspace,
  stopWorkspace,
} from './api/workspaces';
import { type ProvisionConfig, getProvisionConfig } from './api/provision';

// Shared workspace list state + start/stop actions, used by both the list view
// and the detail page. Module-level signals survive route changes and remounts.
export const workspaces = signal<Workspace[]>([]);
export const namespace = signal<string>('');
export const loaded = signal<boolean>(false);
export const error = signal<string | null>(null);
// Usernames with an in-flight start/stop — buttons disabled until the next poll.
export const busy = signal<Set<string>>(new Set());

// Provisioning availability, fetched once on load. null = not yet known; the
// "New workspace" entry point only shows when enabled.
export const provisionConfig = signal<ProvisionConfig | null>(null);

export async function loadProvisionConfig(): Promise<void> {
  try {
    provisionConfig.value = await getProvisionConfig();
  } catch {
    provisionConfig.value = { enabled: false, workspaceDomain: '', githubAppOrg: '' };
  }
}

export function findWorkspace(user: string): Workspace | undefined {
  return workspaces.value.find((w) => w.user === user);
}

export async function refresh(): Promise<void> {
  try {
    const res = await listWorkspaces();
    workspaces.value = res.workspaces;
    namespace.value = res.namespace;
    error.value = null;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loaded.value = true;
  }
}

function setBusy(user: string, on: boolean) {
  const next = new Set(busy.value);
  if (on) next.add(user);
  else next.delete(user);
  busy.value = next;
}

export async function toggle(ws: Workspace): Promise<void> {
  const stopping = ws.state !== 'stopped';
  if (
    stopping &&
    !window.confirm(
      `Stop ${ws.user}? The pod is scaled to zero and its compute freed. ` +
        `The workspace's disk (PVC) is preserved, so you can start it again later.`,
    )
  ) {
    return;
  }
  setBusy(ws.user, true);
  // Optimistic: show transitioning immediately; the poll converges to truth.
  workspaces.value = workspaces.value.map((w) =>
    w.user === ws.user ? { ...w, state: 'transitioning' as WorkspaceState } : w,
  );
  try {
    if (stopping) await stopWorkspace(ws.user);
    else await startWorkspace(ws.user);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    setBusy(ws.user, false);
    await refresh();
  }
}
