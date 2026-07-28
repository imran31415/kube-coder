import { apiGet, apiPost, apiPut, apiDelete } from './client';

/**
 * Typed client for the project registry + brief aggregation API (#464), which
 * backs the AI CTO page. The server stays deterministic — these endpoints only
 * store and aggregate; the CTO chat rides the hypervisor separately (#465).
 */

export type ProjectStatus = 'active' | 'paused' | 'archived';

/** Lightweight per-project pulse embedded on GET /api/projects so the rail
 *  never fans out N×/brief (UX finding F5). */
export interface ProjectPulse {
  running: number;
  waiting: number;
  last_activity_at: number | null;
}

export interface Project {
  id: string;
  name: string;
  workdirs: string[];
  repo: string;
  memory_namespace: string;
  status: ProjectStatus;
  north_star: string;
  last_seen_at: number | null;
  created_at: number;
  updated_at: number;
  pulse?: ProjectPulse;
}

export interface BriefTaskRef {
  task_id: string;
  status: string;
  prompt: string;
  workdir: string;
  assistant?: string;
  last_activity_at: number | null;
}

export interface BriefMemory {
  namespace: string;
  key: string;
  value: string;
  tags: string[];
  importance: number | null;
  updated_at: number | null;
}

export interface BriefGit {
  workdir: string;
  branch: string;
  exists: boolean;
}

export interface BriefTrigger {
  kind: 'webhook' | 'cron';
  id: string;
  workdir?: string;
  schedule?: string;
}

export interface ProjectBrief {
  project: Project;
  tasks: {
    running: number;
    waiting: number;
    total: number;
    recent: BriefTaskRef[];
  };
  goals: BriefMemory[];
  decisions: BriefMemory[];
  memories: BriefMemory[];
  git: BriefGit[];
  triggers: BriefTrigger[];
  counts: { goals: number; decisions: number; memories: number; tasks: number };
  brief_markdown: string;
}

/** A discovery candidate (some auto-registered when confident). */
export interface DiscoverCandidate {
  id: string;
  name: string;
  workdirs: string[];
  repo: string;
  memory_namespace: string;
  reasons: string[];
  confidence: 'high' | 'low';
  registered: boolean;
}

export interface DiscoverResult {
  candidates: DiscoverCandidate[];
  registered: string[];
  /** Ids whose empty `workdirs` discovery healed, so their tasks attribute (#533). */
  backfilled?: string[];
}

export const listProjects = () =>
  apiGet<{ projects: Project[] }>('/api/projects').then((r) => r.projects ?? []);

export const getProject = (id: string) =>
  apiGet<Project>(`/api/projects/${encodeURIComponent(id)}`);

export const createProject = (data: Partial<Project>) =>
  apiPost<Project>('/api/projects', data);

export const updateProject = (id: string, fields: Partial<Project>) =>
  apiPut<Project>(`/api/projects/${encodeURIComponent(id)}`, fields);

export const deleteProject = (id: string) =>
  apiDelete<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}`);

/** Zero-touch auto-provision (#464 UX addendum): the server silently registers
 *  confident candidates and returns the full candidate set. */
export const discoverProjects = (autoProvision = true) =>
  apiPost<DiscoverResult>('/api/projects/_discover', {
    auto_provision: autoProvision,
  });

export const getProjectBrief = (id: string) =>
  apiGet<ProjectBrief>(`/api/projects/${encodeURIComponent(id)}/brief`);
