import type { ComponentChildren } from 'preact';
import { createPortal } from 'preact/compat';

/**
 * Renders `children` into `document.body` so overlays escape the Shell's
 * `.app-content` wrapper.
 *
 * `useFocusTrap` marks `.app-content` `inert` while any modal is open so the
 * background can't be reached by keyboard/AT. `inert` applies to the whole
 * subtree with no escape hatch — so a dialog rendered *inside* `.app-content`
 * (every route-level Drawer/Sheet/Confirm/Prompt/Modal) would inert *itself*
 * and silently become non-interactive (renders, but can't type or click).
 * Portaling to `<body>` makes each overlay a sibling of `.app-content`, matching
 * the Shell-level overlays the inert design already assumes.
 */
export function Portal({ children }: { children: ComponentChildren }) {
  if (typeof document === 'undefined') return null;
  return createPortal(children as never, document.body);
}
