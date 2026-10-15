/**
 * Is the workspace there? (#728)
 *
 * A sleeping or stopped workspace has no pod behind its ingress, so the API
 * answers with ingress-nginx's own HTML 502/503/504 — never server.py's JSON.
 * Once the always-on keeper exists (#728 step 3) it answers in the pod's place
 * with a JSON `workspace_sleeping` error. Anything else — server.py's own JSON
 * 503s included — is the workspace itself talking, so it stays an ordinary
 * error.
 *
 * Pure (no react-native imports) so the node-side vitest suite covers it.
 */
import type { ControllerWorkspace } from '../api/types';
import { parseUrl } from './urlPolicy';

/** awake: answering. unreachable: no answer, cause unknown (asleep or
 *  restarting). asleep: known to be scaled to 0. waking: a wake was requested. */
export type Reach = 'awake' | 'unreachable' | 'asleep' | 'waking';

export const SLEEPING = 'workspace_sleeping';
export const UNREACHABLE = 'workspace_unreachable';
export type DownCode = typeof SLEEPING | typeof UNREACHABLE;

const INGRESS_DOWN_STATUSES = [502, 503, 504];

/** Classify a failed workspace response; null when the workspace answered. */
export function classifyDown(status: number, isJson: boolean, body: unknown): DownCode | null {
  if (isJson) {
    const b = body as { code?: unknown; error?: unknown } | null;
    return b && (b.code === SLEEPING || b.error === SLEEPING) ? SLEEPING : null;
  }
  return INGRESS_DOWN_STATUSES.includes(status) ? UNREACHABLE : null;
}

/** True for an error thrown because the workspace is away (see classifyDown). */
export function isDownError(e: unknown): boolean {
  const code = (e as { code?: unknown } | null | undefined)?.code;
  return code === SLEEPING || code === UNREACHABLE;
}

// Quick at first — a restart is about a minute — then easing off so a long
// sleep costs little battery or data.
const POLL_DELAYS_MS = [2000, 5000, 10000];

/** Delay before health check number `attempt` (0-based) while away. */
export function pollDelay(attempt: number): number {
  return POLL_DELAYS_MS[Math.min(Math.max(attempt, 0), POLL_DELAYS_MS.length - 1)];
}

/**
 * This app's workspace in the controller's list. The controller derives each
 * URL from its WORKSPACE_DOMAIN (`https://<user>.<domain>/`), so a matching
 * hostname is the identity; with no domain configured the URL is null and the
 * host's first label is the user slug.
 */
export function matchWorkspace(
  host: string,
  workspaces: ControllerWorkspace[],
): ControllerWorkspace | undefined {
  const hostname = parseUrl(host)?.hostname;
  if (!hostname) return undefined;
  return (
    workspaces.find((w) => !!w.url && parseUrl(w.url)?.hostname === hostname) ??
    workspaces.find((w) => !w.url && hostname.split('.')[0] === w.user)
  );
}

/** What the controller's view of the Deployment says about it. */
export function reachFromController(w: ControllerWorkspace): Reach {
  if (w.desiredReplicas === 0) return 'asleep';
  return w.readyReplicas > 0 ? 'awake' : 'waking';
}

export interface WakeDeps {
  /** This app's workspace host (config.host). */
  host: string;
  /** Ask the keeper; false when there is no keeper in front of the workspace. */
  viaKeeper: () => Promise<boolean>;
  /** The admin controller connection, when the app has one. */
  controller: null | {
    list: () => Promise<ControllerWorkspace[]>;
    start: (user: string) => Promise<void>;
  };
}

export type WakeResult = { ok: true; via: 'keeper' | 'controller' } | { ok: false; message: string };

export const NO_WAKE_PATH =
  "This app can't wake the workspace yet. Open it in a browser, or connect a controller in Settings.";

/** Wake the workspace: the keeper when it exists (#728 step 3), else the
 *  controller's Start. Never throws — every failure becomes a message. */
export async function wakeWorkspace(d: WakeDeps): Promise<WakeResult> {
  try {
    if (await d.viaKeeper()) return { ok: true, via: 'keeper' };
    if (!d.controller) return { ok: false, message: NO_WAKE_PATH };
    const ws = matchWorkspace(d.host, await d.controller.list());
    if (!ws) return { ok: false, message: "Your controller doesn't list this workspace." };
    await d.controller.start(ws.user);
    return { ok: true, via: 'controller' };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : String(e) };
  }
}
