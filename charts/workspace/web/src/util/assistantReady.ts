/**
 * Readiness of an assistant entry — installed, but is it authenticated? (#702)
 *
 * The server used to DROP an agent whose provider key was missing, so a
 * workspace with DeepSeek installed and no key showed no DeepSeek entry and no
 * explanation: it read as a failed install. It now lists the entry with
 * `ready: false` and the variable name it is waiting for, and these helpers are
 * the one place that turns that pair into the marker and the sentence every
 * surface shows — web pickers and mobile alike — so the New build dialog, the
 * Chat agent picker and the API's own rejection all say the same thing.
 *
 * `ready` absent means ready: an older server sends no such field, and a client
 * must not decide every agent is broken because it is talking to one.
 */

export interface AssistantReadiness {
  id: string;
  label?: string;
  ready?: boolean;
  /** Provider key the entry is waiting for, e.g. `DEEPSEEK_API_KEY`. */
  needs?: string;
}

/** Where the missing key is saved. Also the href of the inline link. */
export const PROVIDER_KEYS_PATH = '/settings/providers#providers';

export function isAssistantReady(a?: AssistantReadiness | null): boolean {
  return a ? a.ready !== false : true;
}

/** Suffix for the entry's line in a picker — '' when it is ready. Deliberately
 *  a suffix rather than a separate column: every picker on both platforms
 *  renders a plain option list, and a native <option> holds no markup. */
export function assistantMarker(a?: AssistantReadiness | null): string {
  return isAssistantReady(a) ? '' : ' · needs API key';
}

/** The sentence shown when a not-ready entry is SELECTED, or null when it is
 *  ready. Names the variable so the user knows which of several keys to save. */
export function missingKeyMessage(a?: AssistantReadiness | null): string | null {
  if (isAssistantReady(a) || !a) return null;
  const label = a.label || a.id;
  const which = a.needs ? ` (${a.needs})` : '';
  return `${label} needs an API key${which}. Add it in Settings → Provider API keys.`;
}
