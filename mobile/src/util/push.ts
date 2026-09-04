/**
 * Pure push-notification helpers — kept RN-free so the node-side vitest can
 * exercise them (the RN glue that imports expo-notifications lives in
 * src/push/notifications.ts and is not imported by tests).
 *
 * A push carries a `data` payload built by the server (push_notify._build_messages):
 * `{ ref, feedId, kind, waiting }`. `ref` is the same typed ref the Feed uses
 * ("task:<id>", "thread:<id>", "memory:..."), so a tap can reuse resolveFeedRef
 * to land on the right screen.
 */
import type { FeedLink } from '../api/types';
import { resolveFeedRef, type FeedRefTarget } from './feed';

/** The `data` blob attached to a push notification (mirror of the server side). */
export interface PushData {
  ref?: string;
  feedId?: string;
  kind?: string;
  waiting?: boolean;
}

/** Resolve a push's data payload to a navigation target, reusing the Feed's ref
 *  mapping. An empty/unknown ref yields `none`, which the caller routes to the
 *  Feed so a tap is never a dead end. */
export function pushTargetFromData(data: PushData | null | undefined): FeedRefTarget {
  const ref = (data?.ref || '').trim();
  if (!ref) return { kind: 'none' };
  return resolveFeedRef({ ref } as FeedLink);
}
