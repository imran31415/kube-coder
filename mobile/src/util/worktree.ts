/**
 * Pure helpers for isolated Builds (#701), kept out of the screens so vitest
 * (node environment, no React Native) can cover them.
 */
import type { WorkdirOption, WorktreeStat } from '../api/types';

/** "3 files changed (+40 −12)" — the same wording as the web dashboard. */
export function formatDiffStat(
  s: Pick<WorktreeStat, 'files_changed' | 'insertions' | 'deletions'> | null | undefined,
): string {
  if (!s) return '';
  if (!s.files_changed) return 'No changes';
  const files = `${s.files_changed} file${s.files_changed === 1 ? '' : 's'} changed`;
  if (!s.insertions && !s.deletions) return files;
  return `${files} (+${s.insertions} −${s.deletions})`;
}

function norm(p: string): string {
  return p.trim().replace(/\/+$/, '');
}

/**
 * Whether `path` can be isolated: it is one of the listed git folders, or a
 * folder inside one. The workdir field is free text on mobile, so a typed
 * sub-folder (`/home/dev/app/web`) counts too. Unknown paths answer false —
 * the switch is only offered where the server will accept it.
 */
export function isGitWorkdir(path: string, dirs: WorkdirOption[]): boolean {
  const p = norm(path);
  if (!p) return false;
  return dirs.some((d) => {
    if (!d.is_git_repo) return false;
    const root = norm(d.path);
    return p === root || p.startsWith(`${root}/`);
  });
}

export type RemoveErrorKind = 'dirty' | 'live' | 'other';

/** How the Remove flow should react to a failed removal. */
export function removeErrorKind(err: unknown): RemoveErrorKind {
  const code = (err as { code?: unknown } | null)?.code;
  if (code === 'dirty') return 'dirty';
  if (code === 'live') return 'live';
  return 'other';
}

/** Whether a task (list row or raw task.json detail) says its worktree is gone. */
export function worktreeRemoved(w: { removed?: boolean; removed_at?: number | null } | null | undefined): boolean {
  return Boolean(w && (w.removed || typeof w.removed_at === 'number'));
}
