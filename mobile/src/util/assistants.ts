/** Readiness helpers for server-listed assistants (#702) — same rules as the
 *  web SPA's util/assistants.ts. An entry can be listed (installed) but not
 *  ready (e.g. no API key yet); only an explicit `ready: false` counts, so an
 *  older server reads as all-ready. */

export interface ReadinessFields {
  id: string;
  label?: string;
  default?: boolean;
  ready?: boolean;
  notReadyReason?: string;
}

export function isNotReady(a: ReadinessFields | undefined | null): boolean {
  return a?.ready === false;
}

/** The entry a picker should pre-select: the flagged default if it's ready,
 *  else the first ready entry, else the first entry at all. */
export function pickDefault<T extends ReadinessFields>(list: T[]): T | undefined {
  return (
    list.find((a) => a.default && !isNotReady(a)) ??
    list.find((a) => !isNotReady(a)) ??
    list[0]
  );
}

/** `wanted` when it names a ready entry, else the ready-aware default's id.
 *  An id the list doesn't know is kept as-is. */
export function readyOr<T extends ReadinessFields>(list: T[], wanted: string): string {
  const hit = list.find((a) => a.id === wanted);
  if (hit && !isNotReady(hit)) return wanted;
  if (!hit && wanted) return wanted;
  return pickDefault(list)?.id ?? wanted;
}

/** Why the selected assistant can't start, or null when it can. */
export function notReadyMessage(a: ReadinessFields | undefined | null): string | null {
  if (!a || !isNotReady(a)) return null;
  return a.notReadyReason || `${a.label || a.id} isn't set up yet.`;
}
