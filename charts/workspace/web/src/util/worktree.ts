/**
 * Small, pure helpers for isolated Builds (#701), shared by the task list,
 * the Changes tab, Settings → Worktrees and the Board review card so every
 * surface words the same numbers the same way.
 */
import type { WorktreeStat, WorktreeStatus } from '../api/tasks';

type Counts = Pick<WorktreeStat | WorktreeStatus, 'files_changed' | 'insertions' | 'deletions'>;

/** "3 files changed (+40 −12)", "1 file changed", "No changes". */
export function diffStatLabel(s: Counts | null | undefined): string {
  if (!s) return '';
  if (!s.files_changed) return 'No changes';
  const files = `${s.files_changed} file${s.files_changed === 1 ? '' : 's'} changed`;
  if (!s.insertions && !s.deletions) return files;
  return `${files} (+${s.insertions} −${s.deletions})`;
}

/**
 * Whether a Board run would put several agents in ONE checkout: a repository
 * is set, isolation is off, and more than one item runs at once. That is the
 * collision #701 exists to prevent, so the Runs form warns before it starts.
 */
export function shouldWarnSharedTree(workdir: string, isolate: boolean, concurrency: number): boolean {
  return Boolean(workdir) && !isolate && concurrency > 1;
}

export type WorktreeState = 'live' | 'finished' | 'orphaned' | 'manual';

/** How Settings labels a worktree's owner. */
export function worktreeStateOf(row: { task_id: string; live: boolean; owner_status: string }): WorktreeState {
  if (!row.task_id) return 'manual';
  if (row.live) return 'live';
  if (!row.owner_status) return 'orphaned';
  return 'finished';
}

/** Human wording for the cleanup sweep's keep reasons. */
export const KEEP_REASON_LABEL: Record<string, string> = {
  live: 'its Build is running',
  recent: 'recently used',
  not_terminal: 'its Build has not finished',
  dirty: 'has uncommitted changes',
  unpushed: 'has commits not on any remote',
  repo_missing: 'its repository is gone',
  error: 'could not be checked',
};

/** Short SHA for display. */
export const sha7 = (sha: string | null | undefined) => (sha ? sha.slice(0, 7) : '');
