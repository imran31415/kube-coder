// Geometry for the AI CTO page's draggable projects-rail splitter (#466).
// Same shape as the Hypervisor's sidebarSplit — pure, unit-testable logic; the
// pointer plumbing stays in the route component.

/** localStorage key for the persisted rail width (px). */
export const CTO_RAIL_W_KEY = 'kc.ctoRailW';
/** Default projects-rail track width. */
export const CTO_RAIL_W_DEFAULT = 248;
/** Narrow enough to reclaim space, still fits a project card. */
export const CTO_RAIL_W_MIN = 190;
/** Wide enough for long project names without dwarfing the chat. */
export const CTO_RAIL_W_MAX = 420;

/** Clamp a dragged width to the allowed track range. */
export function clampCtoRailW(px: number): number {
  return Math.min(CTO_RAIL_W_MAX, Math.max(CTO_RAIL_W_MIN, px));
}

/** Parse a persisted width; anything unparsable or out of range → default. */
export function initialCtoRailW(raw: string | null): number {
  const v = parseFloat(raw ?? '');
  return v >= CTO_RAIL_W_MIN && v <= CTO_RAIL_W_MAX ? v : CTO_RAIL_W_DEFAULT;
}
