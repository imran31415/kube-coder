import type { HypervisorThread } from '../api/types';

/** The Workspace scope contains unbound CTO chats, not every project's chats. */
export function ctoThreads(list: HypervisorThread[], project: string | null): HypervisorThread[] {
  return list.filter(t => t.persona === 'cto' && (t.project_id || null) === project);
}

/** Preserve the user's selection unless it was the archived conversation. */
export function selectionAfterArchive(active: string | null, archived: string, remaining: HypervisorThread[]): string | null {
  if (active !== archived) return active;
  return remaining.find(t => t.id !== archived && !t.deleted_at)?.id ?? null;
}
