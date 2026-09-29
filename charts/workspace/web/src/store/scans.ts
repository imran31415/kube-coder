import { signal } from '@preact/signals';
import { subscribeEvents, type DashboardEvent } from '../api/events';
import {
  listScans,
  getScan,
  listScanTargets,
  createScan,
  stopScan,
  deleteScan,
  setFindingDisposition,
  getScanConnection,
  saveScanConnection,
  clearScanConnection,
  testScanConnection,
  startScanSignIn,
  submitScanSignIn,
  pollScanSignIn,
  cancelScanSignIn,
  signOutScanSubscription,
  isLiveScan,
  type ConnectionView,
  type CreateScanBody,
  type ScanDetail,
  type ScanSummary,
  type ScanTarget,
} from '../api/scans';
import { pushToast } from './ui';

/**
 * Security scan state (#726).
 *
 * Two update paths, on purpose:
 *
 * * **Events** carry progress while a scan runs. The scanner rewrites its
 *   findings on every one it confirms, so `scan.finding` / `scan.usage` arrive
 *   continuously and the open scan is re-fetched when one lands for it.
 * * **A slow poll** is the safety net for when the stream is down. It only
 *   runs while something is live, because a finished scan never changes again
 *   and polling one is pure waste.
 *
 * Nothing here polls when there is no live scan and nothing is on screen.
 */

export const scans = signal<ScanSummary[]>([]);
export const scansLoaded = signal(false);
export const scansError = signal<string | null>(null);

export const openScan = signal<ScanDetail | null>(null);
export const openScanError = signal<string | null>(null);

export const targets = signal<ScanTarget[]>([]);
export const connection = signal<ConnectionView | null>(null);
export const connectionBusy = signal(false);
export const connectionResult = signal<{ ok: boolean; detail: string } | null>(null);

/** True once the surface is known to be unavailable on this workspace. */
export const scansDisabled = signal(false);

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** A 503 from every route means the deployment has scanning switched off. */
function noteDisabled(err: unknown): void {
  scansDisabled.value = /switched off/i.test(describe(err));
}

export async function loadScans(quiet = false): Promise<void> {
  try {
    const res = await listScans();
    scans.value = res.scans;
    scansError.value = null;
    scansDisabled.value = false;
  } catch (err) {
    noteDisabled(err);
    // Keep the last good list: a transient failure must not blank the page and
    // suggest the user's scan history disappeared.
    if (!quiet) scansError.value = describe(err);
  } finally {
    scansLoaded.value = true;
  }
}

export async function loadScan(id: string, quiet = false): Promise<void> {
  try {
    const detail = await getScan(id);
    openScan.value = detail;
    openScanError.value = null;
  } catch (err) {
    if (!quiet) openScanError.value = describe(err);
  }
}

export async function loadTargets(): Promise<void> {
  try {
    targets.value = (await listScanTargets()).targets;
  } catch (err) {
    noteDisabled(err);
    targets.value = [];
  }
}

export async function loadConnection(): Promise<void> {
  try {
    connection.value = await getScanConnection();
    scansDisabled.value = false;
  } catch (err) {
    noteDisabled(err);
  }
}

export async function saveConnection(body: {
  model?: string;
  api_key?: string;
  api_base?: string;
}): Promise<boolean> {
  connectionBusy.value = true;
  connectionResult.value = null;
  try {
    connection.value = await saveScanConnection(body);
    return true;
  } catch (err) {
    connectionResult.value = { ok: false, detail: describe(err) };
    return false;
  } finally {
    connectionBusy.value = false;
  }
}

export async function forgetConnection(): Promise<void> {
  connectionBusy.value = true;
  try {
    connection.value = await clearScanConnection();
    connectionResult.value = null;
  } catch (err) {
    pushToast(describe(err), { kind: 'danger', ttl: 6000 });
  } finally {
    connectionBusy.value = false;
  }
}

export async function testConnection(): Promise<void> {
  connectionBusy.value = true;
  connectionResult.value = null;
  try {
    connectionResult.value = await testScanConnection();
  } catch (err) {
    connectionResult.value = { ok: false, detail: describe(err) };
  } finally {
    connectionBusy.value = false;
  }
}

// ---- subscription sign-in -------------------------------------------------

/** The link the user opens in their own browser, once a sign-in has begun. */
export const signInUrl = signal<string | null>(null);
export const signInError = signal<string | null>(null);
export const signInBusy = signal(false);

let signInPoll: number | null = null;

function stopSignInPoll(): void {
  if (signInPoll !== null) window.clearInterval(signInPoll);
  signInPoll = null;
}

/**
 * Begin a subscription sign-in.
 *
 * This pod cannot receive the sign-in callback — the browser is on the user's
 * own machine — so the workspace hands over a link and then waits. Polling
 * starts here rather than when the user pastes, because the sign-in can also
 * fail before that (a link that never loaded, a session that died).
 */
export async function beginSignIn(): Promise<void> {
  signInBusy.value = true;
  signInError.value = null;
  try {
    const res = await startScanSignIn();
    signInUrl.value = res.url;
    stopSignInPoll();
    signInPoll = window.setInterval(() => void pollSignIn(), 2000);
  } catch (err) {
    signInError.value = describe(err);
  } finally {
    signInBusy.value = false;
  }
}

/** Hand over what the user pasted. Nothing here keeps a copy. */
export async function submitSignIn(redirect: string): Promise<void> {
  signInBusy.value = true;
  signInError.value = null;
  try {
    await submitScanSignIn(redirect);
  } catch (err) {
    signInError.value = describe(err);
  } finally {
    signInBusy.value = false;
  }
}

async function pollSignIn(): Promise<void> {
  try {
    const res = await pollScanSignIn();
    if (res.signed_in) {
      stopSignInPoll();
      signInUrl.value = null;
      if (res.connection) connection.value = res.connection;
      else await loadConnection();
      return;
    }
    if (!res.in_progress) {
      stopSignInPoll();
      signInUrl.value = null;
      signInError.value = res.error ?? 'The sign-in did not complete.';
    }
  } catch (err) {
    stopSignInPoll();
    signInError.value = describe(err);
  }
}

export async function abandonSignIn(): Promise<void> {
  stopSignInPoll();
  signInUrl.value = null;
  signInError.value = null;
  try {
    await cancelScanSignIn();
  } catch {
    /* the session times out on its own */
  }
}

export async function signOutSubscription(): Promise<void> {
  connectionBusy.value = true;
  try {
    const res = await signOutScanSubscription();
    connection.value = res.connection;
  } catch (err) {
    pushToast(describe(err), { kind: 'danger', ttl: 6000 });
  } finally {
    connectionBusy.value = false;
  }
}

/** Start a scan. Returns its id, or null with a toast explaining why not. */
export async function startScan(body: CreateScanBody): Promise<string | null> {
  try {
    const res = await createScan(body);
    await loadScans(true);
    return res.scan_id;
  } catch (err) {
    pushToast(describe(err), { kind: 'danger', ttl: 6000 });
    return null;
  }
}

export async function stop(id: string): Promise<void> {
  try {
    await stopScan(id);
    await Promise.all([loadScans(true), loadScan(id, true)]);
  } catch (err) {
    pushToast(describe(err), { kind: 'danger', ttl: 6000 });
  }
}

export async function remove(id: string): Promise<void> {
  try {
    await deleteScan(id);
    if (openScan.value?.id === id) openScan.value = null;
    await loadScans(true);
  } catch (err) {
    pushToast(describe(err), { kind: 'danger', ttl: 6000 });
  }
}

export async function dismissFinding(
  scanId: string,
  findingId: string,
  disposition: 'open' | 'dismissed',
): Promise<void> {
  try {
    const res = await setFindingDisposition(scanId, findingId, disposition);
    if (openScan.value?.id === scanId) {
      openScan.value = { ...openScan.value, dispositions: res.dispositions };
    }
  } catch (err) {
    pushToast(describe(err), { kind: 'danger', ttl: 6000 });
  }
}

/** Any scan still working — what makes the safety poll worth running. */
export function hasLiveScan(): boolean {
  return scans.value.some(isLiveScan);
}

// ---- live updates ---------------------------------------------------------

let unsubscribe: (() => void) | null = null;
let pollTimer: number | null = null;
let subscribers = 0;

function onEvent(ev: DashboardEvent): void {
  if (!ev.type.startsWith('scan.')) return;
  const id = typeof ev.data.id === 'string' ? ev.data.id : '';
  // A status change reorders and relabels the list; a finding or a cost tick
  // only matters for the scan being looked at.
  if (ev.type === 'scan.status' || ev.type === 'scan.done') {
    void loadScans(true);
  }
  if (id && openScan.value?.id === id) void loadScan(id, true);
}

function tick(): void {
  if (!hasLiveScan()) return;
  void loadScans(true);
  const current = openScan.value;
  if (current && isLiveScan(current)) void loadScan(current.id, true);
}

/**
 * Start receiving updates. Reference-counted, so the route and a detail view
 * can both ask without either one tearing the other's stream down.
 *
 * The poll interval is deliberately slack: events are the real mechanism and
 * this only exists for when the stream is down, so a frequent poll would spend
 * requests to duplicate work already being done.
 */
export function watchScans(): () => void {
  subscribers += 1;
  if (subscribers === 1) {
    unsubscribe = subscribeEvents(onEvent);
    pollTimer = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      tick();
    }, 15000);
  }
  return () => {
    subscribers = Math.max(0, subscribers - 1);
    if (subscribers > 0) return;
    unsubscribe?.();
    unsubscribe = null;
    if (pollTimer !== null) window.clearInterval(pollTimer);
    pollTimer = null;
  };
}

/** Test seam: drop every subscription and reset counters. */
export function _resetScanWatchForTest(): void {
  stopSignInPoll();
  subscribers = 0;
  unsubscribe?.();
  unsubscribe = null;
  if (pollTimer !== null) window.clearInterval(pollTimer);
  pollTimer = null;
}
