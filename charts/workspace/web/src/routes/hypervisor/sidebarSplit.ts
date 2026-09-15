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

// ── Brief pane (#683) ──────────────────────────────────────────────────────
// The right-hand pane that shows the open chat's project brief. The collapse
// machinery below started life on the AI CTO page (#530, routes/cto/railSplit)
// and moved here when the brief did: it is the same idea in both places, and
// keeping one copy is the point of the merge. railSplit re-exports it for as
// long as that page exists.

/** localStorage key for the persisted brief-pane collapse choice. */
export const BRIEF_COLLAPSED_KEY = 'kc.hvBriefCollapsed';

/** Expanded brief track — the width the panel has always had. */
export const BRIEF_W = 320;
/** Collapsed brief — a thin vertical edge tab. */
export const BRIEF_COLLAPSED_W = 34;

/** Below this viewport width the brief auto-collapses: wide enough to render
 *  three columns, too narrow for the chat to be comfortable between them. */
export const BRIEF_AUTO_COLLAPSE_MAX = 1200;

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

/**
 * The desktop grid template: sidebar · handle · chat · (brief). The brief track
 * only exists when there is a brief to show — a chat with no project bound
 * keeps exactly the two-column layout Chat has always had.
 */
export function chatGridTemplate(opts: {
  sidebarW: number;
  brief: 'none' | 'expanded' | 'collapsed';
}): string {
  const base = `${Math.round(opts.sidebarW)}px 6px minmax(0, 1fr)`;
  if (opts.brief === 'none') return base;
  return `${base} ${opts.brief === 'collapsed' ? BRIEF_COLLAPSED_W : BRIEF_W}px`;
}
