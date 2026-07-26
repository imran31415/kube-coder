import { apiGet, apiPost } from './client';

/**
 * Typed client for the Feed backend (#469): the single reverse-chronological
 * stream of briefings, news, activity, and decisions. System items are emitted
 * deterministically by the server; curated items are agent-authored via the
 * post_update MCP tool.
 */

export type FeedKind = 'briefing' | 'news' | 'activity' | 'decision';

export interface FeedLink {
  label: string;
  /** Typed internal ref: task:<id> | thread:<id> | memory:<ns>/<key>. */
  ref?: string;
  /** External URL (opens in a new tab). */
  href?: string;
}

export interface FeedItem {
  id: string;
  ts: number;
  kind: FeedKind;
  title: string;
  body_md: string;
  source: string;
  project_id: string;
  links: FeedLink[];
  waiting: boolean;
  read: boolean;
}

export interface FeedQuery {
  since?: number;
  project?: string;
  kinds?: FeedKind[];
  unread?: boolean;
  limit?: number;
}

export const listFeed = (q: FeedQuery = {}) =>
  apiGet<{ items: FeedItem[] }>('/api/feed', {
    since: q.since,
    project: q.project,
    kinds: q.kinds && q.kinds.length ? q.kinds.join(',') : undefined,
    unread: q.unread ? 1 : undefined,
    limit: q.limit,
  }).then((r) => r.items ?? []);

export const feedUnreadCount = () =>
  apiGet<{ count: number }>('/api/feed/unread_count').then((r) => r.count ?? 0);

export const markFeedRead = (id: string) =>
  apiPost<{ ok: boolean }>(`/api/feed/${encodeURIComponent(id)}/read`);

export const dismissFeedItem = (id: string) =>
  apiPost<{ ok: boolean }>(`/api/feed/${encodeURIComponent(id)}/dismiss`);
