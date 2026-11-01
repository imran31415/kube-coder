/**
 * The wide layout (#833): a desktop-width window gets a permanent sidebar
 * beside the content instead of the ☰ drawer.
 *
 * Desktop app only. A phone, a tablet and a browser keep the layout they have
 * always had — including the web export the store screenshots are taken from
 * (scripts/screenshots.mjs renders an iPad at 1024px, which a bare width rule
 * would have redrawn). Below the breakpoint the desktop app is the phone
 * layout too, so a narrow window behaves like the phone.
 *
 * Pure (no react-native imports) so the node-side vitest walks the boundary;
 * `useWideLayout` (src/components/WideLayout.tsx) feeds it the live window.
 */

/** Narrowest window that gets the sidebar. It leaves the content pane
 *  ~700px, wider than any phone screen the layouts were built for. */
export const WIDE_BREAKPOINT = 960;
/** The sidebar's width in the wide layout. */
export const SIDEBAR_WIDTH = 264;

export function isWideLayout(desktop: boolean, windowWidth: number): boolean {
  return desktop && windowWidth >= WIDE_BREAKPOINT;
}
