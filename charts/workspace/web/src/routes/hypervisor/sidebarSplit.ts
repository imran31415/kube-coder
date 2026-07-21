// Geometry for the Hypervisor's draggable sidebar splitter (#350). On desktop
// the route grid is `<sidebar> <handle> <chat>` and dragging the handle sets
// the sidebar track in px, resizing the chat column against it. The pure
// logic lives here so it's unit-testable; the pointer plumbing stays in the
// route component (mirrors the Build tab's TerminalPane splitter).

/** localStorage key for the persisted sidebar width (px). */
export const SIDEBAR_W_KEY = 'kc.hvSidebarW';
/** Default matches the grid's previous fixed 264px track. */
export const SIDEBAR_W_DEFAULT = 264;
/** Narrow enough to reclaim space, still fits the agent/folder pickers. */
export const SIDEBAR_W_MIN = 200;
/** Wide enough for long thread titles without dwarfing the chat. */
export const SIDEBAR_W_MAX = 480;

/** Clamp a dragged width to the allowed track range. */
export function clampSidebarW(px: number): number {
  return Math.min(SIDEBAR_W_MAX, Math.max(SIDEBAR_W_MIN, px));
}

/** Parse a persisted width; anything unparsable or out of range → default. */
export function initialSidebarW(raw: string | null): number {
  const v = parseFloat(raw ?? '');
  return v >= SIDEBAR_W_MIN && v <= SIDEBAR_W_MAX ? v : SIDEBAR_W_DEFAULT;
}
