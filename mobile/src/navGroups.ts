/**
 * Drawer navigation — mirrors the web dashboard's rail
 * (charts/workspace/web/src/store/router.ts: NAV_TOP / NAV_GROUPS /
 * WORKSPACE_TAIL) so both apps present the same information architecture.
 * Exactly two top-level entries:
 *
 *   Chat       — the home screen (the tab navigator's initial route)
 *   Workspace  — one collapsible entry holding everything else:
 *     Mission Control — agents doing work (Overview, Feed, Board, Builds, …)
 *     Machine         — the machine itself (Desktop, Apps, Files, Metrics)
 *     Knowledge       — what the workspace knows (Memory, Skills, Docs)
 *     (untitled tail) — Controller + Settings, pinned (#549)
 *
 * "Machine" is the old "Workspace" category: once Workspace became the
 * container, a sub-heading with the same name read as a mistake.
 *
 * Mobile-only deltas vs the web: Metrics and Controller exist only here
 * (Metrics is machine health → Machine; the conditional Controller entry
 * stays in the untitled tail next to Settings).
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

/** The first top-level entry, and the app's home screen. */
export const CHAT_ITEM: NavItemDef = { name: 'Hypervisor', label: 'Chat', icon: 'chatbubbles-outline' };

/** The tab navigator's initial route: Chat (was Desktop). */
export const HOME_SCREEN = CHAT_ITEM.name;

/** The second top-level entry: the disclosure over NAV_GROUPS. */
export const WORKSPACE_TITLE = 'Workspace';
export const WORKSPACE_ICON = 'briefcase-outline';

/** The sections inside Workspace, in order; the untitled tail closes it. */
export const NAV_GROUPS: NavGroupDef[] = [
  {
    title: 'Mission Control',
    items: [
      { name: 'MissionControl', label: 'Overview', icon: 'file-tray-full-outline' },
      { name: 'Feed', label: 'Feed', icon: 'newspaper-outline' },
      // Board review (#588). Mission Control, beside Feed: both answer
      // "what needs me?" — Feed for this workspace, Board for someone
      // else's tracker.
      { name: 'Board', label: 'Board', icon: 'clipboard-outline' },
      { name: 'Tasks', label: 'Builds', icon: 'layers-outline' },
      // Security scans (#726) sit here for the same reason Board does: they
      // answer "what needs me?" — Board about someone else's tracker, this
      // about the app the user is building.
      { name: 'Security', label: 'Security', icon: 'shield-checkmark-outline' },
      { name: 'Walkie', label: 'Walkie-Talkie', icon: 'radio-outline' },
      // Triggers fire builds — agent ops, not workspace plumbing (same
      // reasoning as the web rail, which files /triggers under Mission Control).
      { name: 'Triggers', label: 'Triggers', icon: 'flash-outline' },
    ],
  },
  {
    title: 'Machine',
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

/** True when `screen` is reached through the Workspace disclosure — i.e. it
 *  is anything the drawer lists other than Chat. */
export function inWorkspace(screen: string): boolean {
  return NAV_GROUPS.some((g) => g.items.some((i) => i.name === screen));
}

/**
 * Whether Workspace is open — the same rule as the web rail: collapsed by
 * default, open while the active screen lives inside it, and an explicit
 * collapse on such a screen sticks until the user moves elsewhere (so the
 * header can take the accent tint instead and the user keeps their place).
 */
export interface WorkspaceDisclosure {
  /** The user explicitly opened it. */
  open: boolean;
  /** The screen the user explicitly collapsed it on, if any. */
  dismissedOn: string | null;
}

export const WORKSPACE_DEFAULT: WorkspaceDisclosure = { open: false, dismissedOn: null };

export function isWorkspaceExpanded(d: WorkspaceDisclosure, active: string): boolean {
  return d.open || (inWorkspace(active) && d.dismissedOn !== active);
}

export function toggleWorkspace(d: WorkspaceDisclosure, active: string): WorkspaceDisclosure {
  return isWorkspaceExpanded(d, active)
    ? { open: false, dismissedOn: active }
    : { open: true, dismissedOn: null };
}

/** The drawer IA split into the part that scrolls and the tail that does not.
 *
 * Why the tail is pinned (#549): with Workspace open, the destination list is
 * taller than any iPhone screen — 17 entries against a ~580-650pt viewport
 * once the safe-area insets, brand row and footer are taken out — so whatever
 * sits last is off the fold. It used to be worse than "off the fold": the list
 * was a plain View, so on iOS (overflow defaults to `visible`, and hitTest: does not
 * descend into subviews drawn outside their parent's frame) Settings rendered
 * but could not be tapped at all. #542 made the list a ScrollView, which fixes
 * the dead tap, but Settings still lands ~190pt below the fold with no
 * at-rest scroll affordance.
 *
 * Keeping the untitled tail (Controller + Settings) outside the scroller makes
 * it immune to the list growing again — the drawer has gained five entries
 * since it replaced the tab bar. The tail is still part of Workspace: the
 * drawer shows it, pinned under the scrolling sections, whenever Workspace is
 * open. The web rail solved the same overflow the
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
