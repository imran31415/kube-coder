/**
 * Every screen that loads on first open (#833), and where its code lives.
 *
 * Each loader is a literal `import()`, which is what makes Metro split the
 * screen into its own chunk in a web export (`expo export --platform web`):
 * the desktop app and the browser download and parse a screen only the first
 * time it is opened, so an idle desktop window holds the JS of the screens it
 * has shown rather than all of them. On iOS and Android Metro keeps
 * `import()` in the one native bundle, so a phone build behaves exactly as
 * before; the import only defers evaluating the module.
 *
 * The shell stays eager: App, the drawer/sidebar, the wake banner, the home
 * screen (`Desktop`, the tab navigator's first route) and Onboarding, which is
 * what a fresh install opens on.
 *
 * Pure (no react-native imports) so the node-side vitest can check that every
 * route the navigator registers, and every route a deep link opens, has a
 * loader that resolves (src/screenRegistry.test.ts).
 */
import type { ComponentType } from 'react';

export type ScreenModule = { default: ComponentType<any> };

export const screenLoaders = {
  // Top-level tabs.
  Hypervisor: () => import('./screens/HypervisorScreen'),
  Walkie: () => import('./screens/WalkieScreen'),
  MissionControl: () => import('./screens/MissionControlScreen'),
  Feed: () => import('./screens/FeedScreen'),
  Board: () => import('./screens/BoardScreen'),
  Memory: () => import('./screens/MemoryScreen'),
  Files: () => import('./screens/FilesScreen'),
  Skills: () => import('./screens/SkillsScreen'),
  Triggers: () => import('./screens/TriggersScreen'),
  Metrics: () => import('./screens/MetricsScreen'),
  Controller: () => import('./screens/ControllerScreen'),
  Settings: () => import('./screens/SettingsScreen'),
  // Stack routes, named as the stacks in App.tsx name them.
  TaskList: () => import('./screens/TasksScreen'),
  TaskDetail: () => import('./screens/TaskDetailScreen'),
  NewTask: () => import('./screens/NewTaskScreen'),
  AppList: () => import('./screens/AppsScreen'),
  AppView: () => import('./screens/AppViewScreen'),
  ScanList: () => import('./screens/SecurityScreen'),
  ScanDetail: () => import('./screens/ScanDetailScreen'),
  DocsList: () => import('./screens/DocsScreen'),
  DocsArticle: () => import('./screens/DocsArticleScreen'),
} satisfies Record<string, () => Promise<ScreenModule>>;

export type LazyScreenName = keyof typeof screenLoaders;

/** The tab navigator's routes, in drawer order. A tab is either a screen of
 *  its own (a key of `screenLoaders`) or a stack whose screens are. */
export const TAB_ROUTES = [
  'Desktop',
  'Hypervisor',
  'Walkie',
  'MissionControl',
  'Feed',
  'Board',
  'Tasks',
  'Security',
  'Apps',
  'Memory',
  'Files',
  'Skills',
  'Triggers',
  'Docs',
  'Metrics',
  'Controller',
  'Settings',
] as const;

/** The stack tabs and their routes, first route first. */
export const STACK_ROUTES = {
  Tasks: ['TaskList', 'TaskDetail', 'NewTask'],
  Security: ['ScanList', 'ScanDetail'],
  Apps: ['AppList', 'AppView'],
  Docs: ['DocsList', 'DocsArticle'],
} as const satisfies Record<string, readonly LazyScreenName[]>;

/** Screens that are part of the shell and load with it. */
export const EAGER_SCREENS = ['Desktop', 'Onboarding'] as const;

/** The screen a tab opens first: the tab itself, or its stack's first route. */
export function firstScreenOf(tab: string): string {
  const stack = (STACK_ROUTES as Record<string, readonly string[]>)[tab];
  return stack ? stack[0] : tab;
}
