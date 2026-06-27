import { apiGet, apiPost } from './client';

/**
 * Current vs latest workspace version, brokered from the workspace-controller.
 * `available` is false when self-serve updates aren't configured for this
 * deployment — the Updates section then hides itself.
 */
export interface WorkspaceVersion {
  available: boolean;
  reason?: string;
  user?: string;
  version?: string | null;
  imageTag?: string | null;
  latestVersion?: string | null;
  updateAvailable?: boolean;
  state?: string;
  error?: string;
}

export interface UpdateResult {
  ok?: boolean;
  user?: string;
  fromVersion?: string | null;
  toVersion?: string;
  imageTag?: string;
  rolled?: boolean;
  persisted?: boolean;
  persistError?: string | null;
  error?: string;
}

export const getWorkspaceVersion = () => apiGet<WorkspaceVersion>('/api/workspace/version');

export const updateWorkspace = () => apiPost<UpdateResult>('/api/workspace/update', {});
