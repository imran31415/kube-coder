/**
 * Pause every CSS animation while the window is hidden (#833).
 *
 * react-native-web draws ActivityIndicator — the "running" spinner in status
 * pills, Mission Control and the Builds list — as an infinite CSS keyframe
 * rotation. Nothing needs to turn while nobody can see the window, so on web
 * (and in the desktop app's webview) a hidden window marks the root element
 * and one stylesheet rule pauses every animation under it; showing the window
 * removes the mark and they carry on from where they stopped.
 *
 * Written against the few DOM members it uses so the node-side vitest can
 * drive it with a fake document.
 */

export const HIDDEN_ATTR = 'data-kc-hidden';

export const PAUSE_CSS =
  `html[${HIDDEN_ATTR}] *, html[${HIDDEN_ATTR}] *::before, html[${HIDDEN_ATTR}] *::after ` +
  '{ animation-play-state: paused !important; }';

export interface DocLike {
  documentElement: { setAttribute(name: string, value: string): void; removeAttribute(name: string): void };
  head: { appendChild(node: unknown): unknown } | null;
  createElement(tag: 'style'): { textContent: string | null };
}

/** Add the pause rule and follow `subscribe`'s visibility from now on.
 *  Returns the unsubscribe. */
export function installAnimationPause(
  doc: DocLike,
  visibleNow: boolean,
  subscribe: (l: (visible: boolean) => void) => () => void,
): () => void {
  const style = doc.createElement('style');
  style.textContent = PAUSE_CSS;
  doc.head?.appendChild(style);
  const apply = (visible: boolean) => {
    if (visible) doc.documentElement.removeAttribute(HIDDEN_ATTR);
    else doc.documentElement.setAttribute(HIDDEN_ATTR, '');
  };
  apply(visibleNow);
  return subscribe(apply);
}
