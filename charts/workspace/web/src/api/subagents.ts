import { apiGet } from './client';
import type { TaskStatus } from './tasks';

export interface SubagentInvocation {
  tool_use_id: string;
  tool: string;
  timestamp: string | number;
  session_id: string;
  project: string;
  description?: string;
  subagent_type?: string;
  prompt?: string;
  // Mirrors the backend task status (sub-agents are real spawned tasks now),
  // so it carries the full TaskStatus union, not a hand-picked subset.
  status: TaskStatus;
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
