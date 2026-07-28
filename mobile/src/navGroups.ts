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
