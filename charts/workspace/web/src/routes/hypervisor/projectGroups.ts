import type { HypervisorThread } from '../../api/hypervisor';
import type { Project } from '../../api/projects';

/**
 * Groups the chat list by the project each chat is filed into (#358).
 *
 * The binding itself lives on the thread (`project_id`, persisted server-side);
 * this is purely the sidebar's presentation of it. Unfiled chats keep working
 * exactly as before — they collect in a trailing "No project" group, and a
 * workspace where nothing is filed yields a single unlabelled group so the
 * sidebar looks untouched.
 */

export interface ThreadGroup {
  /** Project id, or '' for the unfiled group. */
  id: string;
  /** Display name: the project's `name`, or its id when the registry has no
   *  record for it (a project archived/deleted out from under its chats). */
  label: string;
  threads: HypervisorThread[];
}

/** True when the groups carry no information worth rendering headers for —
 *  everything sits in the single unfiled group. */
export function isUngrouped(groups: ThreadGroup[]): boolean {
  return groups.length <= 1 && !groups[0]?.id;
}

/**
 * Group threads by `project_id`, preserving the input order inside each group
 * (the list arrives newest-first). Project groups come first, ordered by their
 * most recently touched chat, so the project you were just in is at the top;
 * the unfiled group always goes last.
 */
export function groupByProject(
  list: HypervisorThread[],
  projects: Project[],
): ThreadGroup[] {
  const names = new Map(projects.map((p) => [p.id, p.name || p.id]));
  const byId = new Map<string, ThreadGroup>();
  for (const t of list) {
    const id = t.project_id || '';
    let g = byId.get(id);
    if (!g) {
      g = { id, label: id ? names.get(id) ?? id : 'No project', threads: [] };
      byId.set(id, g);
    }
    g.threads.push(t);
  }
  const touched = (g: ThreadGroup) =>
    Math.max(...g.threads.map((t) => t.updated_at ?? t.created_at ?? 0));
  const filed = [...byId.values()].filter((g) => g.id);
  filed.sort((a, b) => touched(b) - touched(a));
  const unfiled = byId.get('');
  return unfiled ? [...filed, unfiled] : filed;
}
