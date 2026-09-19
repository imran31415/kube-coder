/**
 * Readiness of an assistant entry — installed, but is it authenticated? (#702)
 *
 * Mirrors `web/src/util/assistantReady.ts`. The server used to DROP an agent
 * whose provider key was missing, so a workspace with DeepSeek installed and no
 * key showed no DeepSeek chip and no explanation — it read as a failed install.
 * It now lists the entry with `ready: false` plus the variable it is waiting
 * for, and these helpers turn that pair into the marker and the sentence, so
 * the phone says exactly what the dashboard says.
 *
 * `ready` absent means ready: an older server sends no such field, and the app
 * must not decide every agent is broken because it is talking to one.
 */

export interface AssistantReadiness {
  id: string;
  label?: string;
  ready?: boolean;
  /** Provider key the entry is waiting for, e.g. `DEEPSEEK_API_KEY`. */
  needs?: string;
}

export function isAssistantReady(a?: AssistantReadiness | null): boolean {
  return a ? a.ready !== false : true;
}

/** Suffix for the entry's chip — '' when it is ready. */
export function assistantMarker(a?: AssistantReadiness | null): string {
  return isAssistantReady(a) ? '' : ' · needs key';
}

/** The sentence shown when a not-ready entry is SELECTED, or null when ready.
 *  Names the variable so the user knows which of several keys to save. Settings
 *  is a tab on the phone rather than a URL, so the pointer is by name. */
export function missingKeyMessage(a?: AssistantReadiness | null): string | null {
  if (isAssistantReady(a) || !a) return null;
  const label = a.label || a.id;
  const which = a.needs ? ` (${a.needs})` : '';
  return `${label} needs an API key${which}. Add it in Settings → Provider API keys.`;
}
