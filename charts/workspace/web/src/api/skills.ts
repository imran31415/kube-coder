import { apiGet, apiPost } from './client';
import { safeArray } from './shape';

/**
 * Skills API — multi-harness SKILL.md surface (issue #187).
 *
 * A "skill" is a normalized, tool-agnostic record discovered from every
 * supported agent harness (Claude Code, OpenCode, Antigravity, …). The
 * same logical skill present in several harnesses with identical content
 * collapses into one record whose `systems` lists all of them; same name
 * with different content stays as separate "divergent" variants.
 */

export interface SkillSource {
  system: string;
  path: string;
  scope: string;
  updated_at: number;
  shadowed: boolean;
}

export interface SkillRecord {
  name: string;
  description: string;
  body: string;
  scope: 'project' | 'user' | 'plugin' | string;
  systems: string[];
  user_invocable: boolean;
  allowed_tools: string[];
  argument_hint: string;
  sources: SkillSource[];
  fingerprint: string;
  updated_at: number;
}

export interface SkillsListQuery {
  system?: string;
  scope?: string;
  refresh?: number;
}

interface ListResponse {
  skills: SkillRecord[];
  count: number;
}

const coerceSkill = (s: SkillRecord): SkillRecord => ({
  ...s,
  systems: safeArray(s.systems) as string[],
  allowed_tools: safeArray(s.allowed_tools) as string[],
  sources: safeArray(s.sources) as SkillSource[],
});

export const listSkills = (q: SkillsListQuery = {}) =>
  apiGet<ListResponse>('/api/skills', q as Record<string, string | number | undefined>)
    .then((r) => ({ ...r, skills: (safeArray(r.skills) as SkillRecord[]).map(coerceSkill) }));

export interface SkillDetail {
  skill: SkillRecord;
  variants: SkillRecord[];
  divergent: boolean;
}

export const getSkill = (name: string) =>
  apiGet<SkillDetail>(`/api/skills/${encodeURIComponent(name)}`)
    .then((r) => ({
      skill: coerceSkill(r.skill),
      variants: (safeArray(r.variants) as SkillRecord[]).map(coerceSkill),
      divergent: !!r.divergent,
    }));

export interface SkillsStats {
  total: number;
  by_system: Record<string, number>;
  by_scope: Record<string, number>;
  syncer?: Record<string, unknown>;
}

export const skillsStats = () => apiGet<SkillsStats>('/api/skills/stats');

export const scanSkills = () =>
  apiPost<{ status: string; result: Record<string, unknown> }>('/api/skills/_scan');

// ── Cross-tool sync (PR2) ────────────────────────────────────────────────────

export interface SyncTarget {
  system: string;
  scope?: string;
}

export interface SyncSkillInput {
  source_system: string;
  source_scope?: string;
  targets: SyncTarget[];
  force?: boolean;
}

export interface SyncResult {
  name: string;
  source_system: string;
  installed: { system: string; scope: string; path: string }[];
  failed: { system: string; scope: string; error: string }[];
}

export interface SyncConflict {
  system: string;
  existing_fingerprint: string;
}

/** Thrown when a target already holds a DIVERGENT copy (HTTP 409). The caller
 *  can re-issue with force=true to overwrite. */
export class SkillSyncConflictError extends Error {
  conflicts: SyncConflict[];
  constructor(conflicts: SyncConflict[]) {
    super('target has a divergent copy');
    this.name = 'SkillSyncConflictError';
    this.conflicts = conflicts;
  }
}

export const syncSkill = (name: string, input: SyncSkillInput) =>
  apiPost<SyncResult>(`/api/skills/${encodeURIComponent(name)}/sync`, input).catch((err) => {
    // apiPost throws ApiError with .status/.body on non-2xx; surface 409 as a
    // typed conflict so the UI can offer a "force" retry.
    const status = (err as { status?: number })?.status;
    const body = (err as { body?: { code?: string; conflicts?: SyncConflict[] } })?.body;
    if (status === 409 && body?.code === 'conflict') {
      throw new SkillSyncConflictError(safeArray(body.conflicts) as SyncConflict[]);
    }
    throw err;
  });
