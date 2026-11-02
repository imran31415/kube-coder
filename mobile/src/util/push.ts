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

/** Where a tap lands, as one navigator call: the tab, and for a stack tab the
 *  screen inside it. `boardFocus` is parked for the Board before navigating
 *  (see `routeTap` in src/push/tapRouter.ts). Pure, so the tests can check
 *  every target against the screens the navigator loads on demand
 *  (src/screenRegistry.test.ts). */
export type TapRoute =
  | { kind: 'external'; url: string }
  | {
      kind: 'screen';
      tab: string;
      screen?: string;
      params?: Record<string, string>;
      boardFocus?: { boardId: string; itemId: string };
    };

export function tapRoute(target: FeedRefTarget): TapRoute {
  switch (target.kind) {
    case 'task':
      return { kind: 'screen', tab: 'Tasks', screen: 'TaskDetail', params: { id: target.id } };
    case 'thread':
      // Opens THAT chat (#683).
      return { kind: 'screen', tab: 'Hypervisor', params: { openThreadId: target.id } };
    case 'memory':
      return { kind: 'screen', tab: 'Memory' };
    case 'board':
      return {
        kind: 'screen',
        tab: 'Board',
        boardFocus: { boardId: target.boardId, itemId: target.itemId },
      };
    case 'scan':
      // A finished security scan (#726): one scan inside the Security stack.
      return { kind: 'screen', tab: 'Security', screen: 'ScanDetail', params: { id: target.id } };
    case 'external':
      return { kind: 'external', url: target.url };
    default:
      return { kind: 'screen', tab: 'Feed' };
  }
}
