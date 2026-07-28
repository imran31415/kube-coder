/**
 * Categorized drawer navigation (#267) — mirrors the web dashboard's
 * NAV_GROUPS (charts/workspace/web/src/store/router.ts) so both apps present
 * the same information architecture:
 *
 *   Mission Control — agents doing work (Overview, Chat, Builds, Walkie-Talkie)
 *   Workspace       — the machine itself (Desktop, Apps, Files, Metrics)
 *   Knowledge       — what the workspace knows (Memory, Skills)
 *   (untitled tail) — Controller + Settings, standalone like the web rail
 *
 * Mobile-only deltas vs the web: Metrics and Controller exist only here
 * (Metrics is machine health → Workspace; the conditional Controller entry
 * stays ungrouped next to Settings). Triggers and Docs landed in #250 and sit
 * in the same groups the web rail files them under.
 *
 * Pure data — no react-native imports — so the node-side vitest suite can
 * assert the IA without rendering. `icon` values are Ionicons glyph names;
 * NavDrawer casts them (and derives the filled active variant by stripping
 * the `-outline` suffix).
 */

export interface NavItemDef {
  /** react-navigation screen name (tab route). */
  name: string;
  /** Drawer display label — may differ from the screen name (Chat, Builds). */
  label: string;
  /** Ionicons outline glyph name. */
  icon: string;
  /** Only shown when a controller host is configured. */
  controller?: boolean;
}

export interface NavGroupDef {
  /** Section header; null = the untitled tail (Controller / Settings). */
  title: string | null;
  items: NavItemDef[];
}

export const NAV_GROUPS: NavGroupDef[] = [
  {
    title: 'Mission Control',
    items: [
      { name: 'MissionControl', label: 'Overview', icon: 'file-tray-full-outline' },
      { name: 'Cto', label: 'AI CTO', icon: 'compass-outline' },
      { name: 'Feed', label: 'Feed', icon: 'newspaper-outline' },
      { name: 'Hypervisor', label: 'Chat', icon: 'chatbubbles-outline' },
      { name: 'Tasks', label: 'Builds', icon: 'layers-outline' },
      { name: 'Walkie', label: 'Walkie-Talkie', icon: 'radio-outline' },
      // Triggers fire builds — agent ops, not workspace plumbing (same
      // reasoning as the web rail, which files /triggers under Mission Control).
      { name: 'Triggers', label: 'Triggers', icon: 'flash-outline' },
    ],
  },
  {
    title: 'Workspace',
    items: [
      { name: 'Desktop', label: 'Desktop', icon: 'grid-outline' },
      { name: 'Apps', label: 'Apps', icon: 'globe-outline' },
      { name: 'Files', label: 'Files', icon: 'folder-outline' },
      { name: 'Metrics', label: 'Metrics', icon: 'stats-chart-outline' },
    ],
  },
  {
    title: 'Knowledge',
    items: [
      { name: 'Memory', label: 'Memory', icon: 'bookmark-outline' },
      { name: 'Skills', label: 'Skills', icon: 'sparkles-outline' },
      { name: 'Docs', label: 'Docs', icon: 'book-outline' },
    ],
  },
  {
    title: null,
    items: [
      { name: 'Controller', label: 'Controller', icon: 'server-outline', controller: true },
      { name: 'Settings', label: 'Settings', icon: 'settings-outline' },
    ],
  },
];

/** The drawer IA split into the part that scrolls and the tail that does not.
 *
 * Why the tail is pinned (#549): the destination list is taller than any
 * iPhone screen — 16 entries against a ~580-650pt viewport once the safe-area
 * insets, brand row and footer are taken out — so whatever sits last is off
 * the fold. It used to be worse than "off the fold": the list was a plain
 * View, so on iOS (overflow defaults to `visible`, and hitTest: does not
 * descend into subviews drawn outside their parent's frame) Settings rendered
 * but could not be tapped at all. #542 made the list a ScrollView, which fixes
 * the dead tap, but Settings still lands ~190pt below the fold with no
 * at-rest scroll affordance.
 *
 * Keeping the untitled tail (Controller + Settings) outside the scroller makes
 * it immune to the list growing again — the drawer has gained five entries
 * since it replaced the tab bar. The web rail solved the same overflow the
 * other way (#448: Settings flows with the nav rather than pinning), because a
 * desktop sidebar has the height to spare and a permanent scrollbar; a phone
 * drawer has neither.
 *
 * Pure — callers pass the controller flag, so this module stays free of store
 * imports and the invariant stays unit-testable without a RN renderer.
 */
export function splitNavGroups({ controller }: { controller: boolean }): {
  scrolling: NavGroupDef[];
  pinned: NavGroupDef | null;
} {
  const groups = NAV_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((i) => !i.controller || controller),
  })).filter((g) => g.items.length > 0);
  const last = groups[groups.length - 1];
  return last && last.title === null
    ? { scrolling: groups.slice(0, -1), pinned: last }
    : { scrolling: groups, pinned: null };
}
