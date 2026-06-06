import { apiGet } from './client';
import type { TaskSummary } from './tasks';

export interface SubagentInvocation {
  tool_use_id: string;
  tool: string;
  timestamp: string | number;
  session_id: string;
  project: string;
  description?: string;
  subagent_type?: string;
  prompt?: string;
  status: 'running' | 'completed' | 'error' | 'killed';
  ended_at?: string | number;
  is_error?: boolean;
}

export interface SubagentsResponse {
  subagents: SubagentInvocation[];
  count: number;
  running_count: number;
  completed_count: number;
  error_count: number;
  window_days?: number;
}

/**
 * List sub-agents spawned by a parent task.
 * This replaces the old read-only transcript scanner with real child task data.
 * @param parentTaskId - The task ID of the parent/spawning task
 */
export const listSubagents = (parentTaskId?: string): Promise<SubagentsResponse> => {
  const params = parentTaskId ? { parent: parentTaskId } : undefined;
  return apiGet<SubagentsResponse>('/api/subagents', params);
};

/**
 * Convert a list of TaskSummary items (from GET /api/claude/tasks?parent=...)
 * into the SubagentInvocation format used by the SubagentsTab component.
 */
export const taskSummariesToSubagents = (tasks: TaskSummary[]): SubagentInvocation[] =>
  tasks.map((t) => ({
    tool_use_id: t.task_id,
    tool: 'spawn_agent',
    timestamp: t.created_at ?? 0,
    session_id: t.task_id,
    project: 'kube-coder',
    description: (t.prompt ?? '').slice(0, 200),
    subagent_type: (t as any).assistant ?? 'claude',
    prompt: t.prompt ?? '',
    status: t.status === 'killed' ? 'killed' : t.status,
    ended_at: t.finished_at ?? undefined,
    is_error: t.status === 'error',
  }));