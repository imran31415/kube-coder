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
  source?: string;
  workspaceHost?: string;
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

export function tapRoute(
  target: FeedRefTarget,
  opts: { openChanges?: boolean } = {},
): TapRoute {
  switch (target.kind) {
    case 'task':
      return {
        kind: 'screen',
        tab: 'Tasks',
        screen: 'TaskDetail',
        // `openChanges`: a Build publishing push (#710) opens the Build's
        // Changes sheet, where the review and Push & Open PR live.
        params: opts.openChanges ? { id: target.id, tab: 'changes' } : { id: target.id },
      };
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

/** A Build publishing push (#710). */
export function isPublishPush(data: PushData | null | undefined): boolean {
  return data?.source === 'build-publish';
}

const normHost = (h: string | undefined) => (h || '').trim().replace(/\/+$/, '');

/** Whether a push belongs to a workspace other than the one this app is
 *  connected to now. Only Build publishing pushes (#710) name their sender: a
 *  Build id means nothing in another workspace, so such a tap must not open
 *  it, nor mark the row read here, where the row does not exist. One with no
 *  sender (a phone that registered before #710) counts as foreign too. Every
 *  other push is taken as this workspace's, as before. */
export function pushFromOtherWorkspace(
  data: PushData | null | undefined,
  connectedHost: string,
): boolean {
  if (!isPublishPush(data)) return false;
  const from = normHost(data?.workspaceHost);
  return !from || from !== normHost(connectedHost);
}

/** Where a push tap lands, given the workspace the app is connected to:
 *  `tapRoute` of its ref, except that a Build publishing push opens the Build
 *  on its Changes sheet, and one from another workspace opens the Feed. */
export function pushTapRoute(data: PushData | null | undefined, connectedHost: string): TapRoute {
  if (pushFromOtherWorkspace(data, connectedHost)) return { kind: 'screen', tab: 'Feed' };
  return tapRoute(pushTargetFromData(data), { openChanges: isPublishPush(data) });
}
