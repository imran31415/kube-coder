import { apiDelete, apiGet, apiPost } from './client';
import type { WorktreeRemoved, WorktreeStat } from './tasks';

/** One isolated worktree on the workspace disk (Settings → Worktrees, #701). */
export interface WorktreeRow {
  /** The repository's folder under ~/.worktrees. */
  repo: string;
  slug: string;
  path: string;
  branch: string;
  port: number | null;
  source_root: string;
  /** The Build that owns it; '' for one made by hand with the worktree skill. */
  task_id: string;
  owner_name: string;
  owner_status: string;
  live: boolean;
  created_by: string;
  created_at: number | null;
  stat: WorktreeStat | null;
  /** Why the last cleanup pass kept it ('live', 'dirty', 'unpushed', …), or ''. */
  keep_reason: string;
}

export interface WorktreeSweepStatus {
  running: boolean;
  last_run_at: number | null;
  removed: number;
  kept: number;
  error: string;
}

export interface WorktreeList {
  worktrees: WorktreeRow[];
  count: number;
  max: number;
  root: string;
  available: boolean;
  sweep: WorktreeSweepStatus;
}

export interface SweepReport {
  removed: { path: string; slug: string; reason: string; task_id: string }[];
  kept: { path: string; slug: string; reason: string }[];
  at: number;
  dry_run: boolean;
}

export const listWorktrees = () => apiGet<WorktreeList>('/api/worktrees');

export const removeWorktree = (repo: string, slug: string, force = false) =>
  apiDelete<WorktreeRemoved>(
    `/api/worktrees/${encodeURIComponent(repo)}/${encodeURIComponent(slug)}${force ? '?force=1' : ''}`,
  );

export const sweepWorktrees = (dryRun = false) =>
  apiPost<SweepReport>('/api/worktrees/sweep', { dry_run: dryRun });
