import { apiGet, apiPost, apiDelete } from './client';

export type TriggerKind = 'webhook' | 'cron';

export interface WebhookRecord {
  id: string;
  prompt_template: string;
  workdir?: string;
  interpolate_mode?: 'attach' | 'interpolate';
  source?: string;
  created_at?: number;
  secret_set?: boolean;
}

export interface CronRecord {
  id: string;
  schedule: string;
  prompt_template: string;
  workdir?: string;
  payload?: Record<string, unknown>;
  interpolate_mode?: 'attach' | 'interpolate';
  timezone?: string;
  suspended?: boolean;
  created_at?: number;
  fire_token_set?: boolean;
  last_fire_at?: number;
}

export interface Trigger {
  kind: TriggerKind;
  id: string;
  name: string;
  schedule?: string;       // cron only
  prompt: string;
  workdir?: string;
  timezone?: string;
  suspended?: boolean;
  created_at?: number;
}

export async function listTriggers(): Promise<Trigger[]> {
  const [wh, cr] = await Promise.all([
    apiGet<{ webhooks: WebhookRecord[] }>('/api/webhooks').catch(() => ({ webhooks: [] as WebhookRecord[] })),
    apiGet<{ crons: CronRecord[] }>('/api/crons').catch(() => ({ crons: [] as CronRecord[] })),
  ]);
  const triggers: Trigger[] = [];
  for (const w of wh.webhooks) {
    triggers.push({
      kind: 'webhook',
      id: w.id,
      name: w.id,
      prompt: w.prompt_template,
      workdir: w.workdir,
      created_at: w.created_at,
    });
  }
  for (const c of cr.crons) {
    triggers.push({
      kind: 'cron',
      id: c.id,
      name: c.id,
      schedule: c.schedule,
      prompt: c.prompt_template,
      workdir: c.workdir,
      timezone: c.timezone,
      suspended: c.suspended,
      created_at: c.created_at,
    });
  }
  triggers.sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
  return triggers;
}

export interface CreateCronInput {
  id: string;
  schedule: string;
  prompt_template: string;
  workdir?: string;
  timezone?: string;
}

export const createCron = (input: CreateCronInput) =>
  apiPost<CronRecord>('/api/crons', input);

export const fireCron = (id: string) => apiPost<{ ok: true }>(`/api/crons/${id}/run`, {});
export const suspendCron = (id: string) => apiPost<{ ok: true }>(`/api/crons/${id}/suspend`, {});
export const resumeCron = (id: string) => apiPost<{ ok: true }>(`/api/crons/${id}/resume`, {});
export const deleteCron = (id: string) => apiDelete<{ ok: true }>(`/api/crons/${id}`);

export interface CreateWebhookInput {
  id: string;
  prompt_template: string;
  workdir?: string;
  interpolate_mode?: 'attach' | 'interpolate';
}

export const createWebhook = (input: CreateWebhookInput) =>
  apiPost<WebhookRecord & { url?: string; secret?: string }>('/api/webhooks', input);

export const testWebhook = (id: string) => apiPost<{ ok: true }>(`/api/webhooks/${id}/test`, {});
export const deleteWebhook = (id: string) => apiDelete<{ ok: true }>(`/api/webhooks/${id}`);
