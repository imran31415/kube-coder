import { apiGet, apiPost } from './client';
export interface PublishStatus {
  supported: boolean;
  eligibility: { can_prepare: boolean; can_publish: boolean; reason: { code: string; message: string } | null };
  preparing: string | null;
  preparation_error: { code: string; message: string } | null;
  preparation: null | {
    id: string; fingerprint: string; draft_revision: number;
    title: string; body: string; draft: boolean; summary_status: string; summary_error?: string;
    author: { name: string; email: string }; checks: { state: string };
    files: { path: string; added: number | null; deleted: number | null; binary: boolean }[];
    destination: { head_repo: string; head_branch: string; base_repo: string; base_branch: string; identity: string };
  };
  operation: null | { id: string; stage: string; preparation_id?: string; commit_sha?: string; error?: { code: string; message: string } };
  pr: null | { number: number; url: string; state: string; draft: boolean; head_sha: string };
}
const path = (id: string) => `/api/claude/tasks/${encodeURIComponent(id)}/publish`;
export const getPublish = (id: string) => apiGet<PublishStatus>(path(id));
export const publishAction = (id: string, action: string, body: unknown = {}) => apiPost<PublishStatus>(path(id) + (action ? '/' + action : ''), body);
export const getPublishDiff = (id: string, preparation: string, file: string) => apiGet<{ diff: string; truncated: boolean }>(path(id) + '/diff', { preparation_id: preparation, file });
export const publishing = (s: PublishStatus | null) => !!s?.operation && !['failed', 'published'].includes(s.operation.stage);
