import { apiGet } from './client';

/** Reveal of the persistent admin API token (see controller.py /api/admin/token).
 *  `enabled` is false when the operator hasn't turned the token on, in which
 *  case `token` is null and the console shows setup guidance instead. */
export interface AdminTokenResponse {
  enabled: boolean;
  token: string | null;
}

export const getAdminToken = () => apiGet<AdminTokenResponse>('/api/admin/token');
