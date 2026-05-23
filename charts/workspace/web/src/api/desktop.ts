import { apiGet, apiPost, apiDelete, api } from './client';

/** Action types backed by `DesktopManager._ALLOWED_ACTION_TYPES` server-side. */
export type DesktopActionType = 'task' | 'url' | 'shell';

export interface DesktopActionTask {
  type: 'task';
  prompt: string;
  workdir?: string;
  assistant?: string;
}
export interface DesktopActionUrl {
  type: 'url';
  url: string;
  target: 'blank' | 'self';
}
export interface DesktopActionShell {
  type: 'shell';
  command: string;
  timeout?: number;
}
export type DesktopAction = DesktopActionTask | DesktopActionUrl | DesktopActionShell;

export interface DesktopItem {
  id: string;
  label: string;
  icon: string;
  hotkey?: string;
  action: DesktopAction;
}

export interface DesktopItemDraft {
  label: string;
  icon: string;
  hotkey?: string;
  action: DesktopAction;
}

export const listDesktop = () => apiGet<{ items: DesktopItem[] }>('/api/desktop');

export const createDesktopItem = (draft: DesktopItemDraft) =>
  apiPost<DesktopItem>('/api/desktop', draft);

export const updateDesktopItem = (id: string, draft: DesktopItemDraft) =>
  api<DesktopItem>(`/api/desktop/${id}`, { method: 'POST', body: draft });

export const deleteDesktopItem = (id: string) =>
  apiDelete<{ ok: true }>(`/api/desktop/${id}`);

export const reorderDesktop = (orderedIds: string[]) =>
  apiPost<{ items: DesktopItem[] }>('/api/desktop/_reorder', { order: orderedIds });

export type LaunchResult =
  | { kind: 'task'; task_id: string }
  | { kind: 'shell'; exit_code: number; stdout: string; stderr: string }
  | { kind: 'url'; url: string; target: 'blank' | 'self' };

export const launchDesktopItem = (id: string) =>
  apiPost<LaunchResult>(`/api/desktop/${id}/launch`, {});
