import { apiGet } from './client';

export interface SubagentInvocation {
  tool_use_id: string;
  tool: string;
  timestamp: string;
  session_id: string;
  project: string;
  description?: string;
  subagent_type?: string;
  prompt?: string;
  status: 'running' | 'completed' | 'error';
  ended_at?: string;
  is_error?: boolean;
}

export interface SubagentsResponse {
  subagents: SubagentInvocation[];
  count: number;
  running_count: number;
  completed_count: number;
  error_count: number;
  window_days: number;
}

export const listSubagents = () => apiGet<SubagentsResponse>('/api/subagents');
