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

// ── Collapsible side panes (#530) ──────────────────────────────────────────
// Both side panes can fold away so the chat — the column the user actually
// works in — owns the width. Each pane's *explicit* choice is persisted next
// to the rail width; with no choice recorded, a width heuristic decides.

/** localStorage keys for the persisted collapse choice of each side pane. */
export const CTO_RAIL_COLLAPSED_KEY = 'kc.ctoRailCollapsed';
export const CTO_BRIEF_COLLAPSED_KEY = 'kc.ctoBriefCollapsed';

/** Collapsed projects rail — an icon strip of project initials. */
export const CTO_RAIL_COLLAPSED_W = 48;
/** Collapsed brief — a thin vertical edge tab. */
export const CTO_BRIEF_COLLAPSED_W = 34;
/** Expanded brief track (the width the panel has always had). */
export const CTO_BRIEF_W = 320;

/** Below this viewport width both panes auto-collapse. Above the 860px stacked
 *  breakpoint there was a wide band where all three panes rendered and the chat
 *  was left uncomfortably narrow. */
export const CTO_AUTO_COLLAPSE_MAX = 1200;

/** Parse a persisted pane choice. `null` = never chosen → heuristics decide. */
export function readPaneCollapsed(raw: string | null): boolean | null {
  if (raw === '1') return true;
  if (raw === '0') return false;
  return null;
}

/** Serialize a pane choice for localStorage. */
export function writePaneCollapsed(collapsed: boolean): string {
  return collapsed ? '1' : '0';
}

/** Resolve a pane's collapsed state — an explicit user choice always wins over
 *  the auto-collapse heuristic. */
export function resolvePaneCollapsed(choice: boolean | null, auto: boolean): boolean {
  return choice ?? auto;
}

/** The desktop grid template: rail · (handle) · chat · brief. The handle track
 *  only exists while the rail is expanded — a collapsed rail isn't resizable. */
export function ctoGridTemplate(opts: {
  railW: number;
  railCollapsed: boolean;
  briefCollapsed: boolean;
}): string {
  const rail = opts.railCollapsed ? CTO_RAIL_COLLAPSED_W : Math.round(opts.railW);
  const brief = opts.briefCollapsed ? CTO_BRIEF_COLLAPSED_W : CTO_BRIEF_W;
  const handle = opts.railCollapsed ? '' : '6px ';
  return `${rail}px ${handle}minmax(0, 1fr) ${brief}px`;
}
