/**
 * The screens that load on first open (#833): every route the navigator
 * registers still resolves to its screen, nothing loads before it is asked
 * for, and a deep link or notification tap reaches a screen whose chunk has
 * never been loaded.
 *
 * The screen modules import react-native, which this node-side suite cannot
 * load, so each is mocked with a stand-in that records when it is loaded.
 * The top-level import of the registry below loads no screen (its loaders
 * are never called on it); the tests call the fresh copy `beforeEach` makes.
 * What is under test is the wiring: the registry, App.tsx and the tap router.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const loaded: string[] = [];
const SCREEN_FILES = [
  'HypervisorScreen', 'WalkieScreen', 'MissionControlScreen', 'FeedScreen', 'BoardScreen',
  'MemoryScreen', 'FilesScreen', 'SkillsScreen', 'TriggersScreen', 'MetricsScreen',
  'ControllerScreen', 'SettingsScreen', 'TasksScreen', 'TaskDetailScreen', 'NewTaskScreen',
  'AppsScreen', 'AppViewScreen', 'SecurityScreen', 'ScanDetailScreen', 'DocsScreen',
  'DocsArticleScreen',
];

import { refFromDeepLink } from './desktop/deepLink';
import {
  EAGER_SCREENS,
  STACK_ROUTES,
  TAB_ROUTES,
  firstScreenOf,
  screenLoaders as registeredLoaders,
  type LazyScreenName,
} from './screenRegistry';
import { pushTargetFromData, tapRoute } from './util/push';

/** The file each route's screen lives in — App.tsx's mapping before #833. */
const EXPECTED_FILE: Record<LazyScreenName, string> = {
  Hypervisor: 'HypervisorScreen',
  Walkie: 'WalkieScreen',
  MissionControl: 'MissionControlScreen',
  Feed: 'FeedScreen',
  Board: 'BoardScreen',
  Memory: 'MemoryScreen',
  Files: 'FilesScreen',
  Skills: 'SkillsScreen',
  Triggers: 'TriggersScreen',
  Metrics: 'MetricsScreen',
  Controller: 'ControllerScreen',
  Settings: 'SettingsScreen',
  TaskList: 'TasksScreen',
  TaskDetail: 'TaskDetailScreen',
  NewTask: 'NewTaskScreen',
  AppList: 'AppsScreen',
  AppView: 'AppViewScreen',
  ScanList: 'SecurityScreen',
  ScanDetail: 'ScanDetailScreen',
  DocsList: 'DocsScreen',
  DocsArticle: 'DocsArticleScreen',
};

const isLazy = (name: string): name is LazyScreenName => name in registeredLoaders;

/** The registry as a fresh module graph sees it: each test starts with no
 *  screen loaded. (A hoisted vi.mock factory runs once per file, so it could
 *  not say whether a later test loaded a screen.) */
let screenLoaders = registeredLoaders;

async function load(name: string): Promise<string> {
  if (!isLazy(name)) throw new Error(`${name} has no loader`);
  const mod = await screenLoaders[name]();
  expect(typeof mod.default).toBe('function');
  return (mod.default as unknown as { file: string }).file;
}

const app = readFileSync(path.join(__dirname, '..', 'App.tsx'), 'utf8');

beforeEach(async () => {
  vi.resetModules();
  loaded.length = 0;
  for (const file of SCREEN_FILES) {
    vi.doMock(`./screens/${file}`, () => {
      loaded.push(file);
      return { default: Object.assign(() => null, { file }) };
    });
  }
  ({ screenLoaders } = await import('./screenRegistry'));
});

describe('the lazy screen registry', () => {
  it('maps every route to the screen it always opened', async () => {
    for (const [name, file] of Object.entries(EXPECTED_FILE)) {
      expect(await load(name), name).toBe(file);
    }
    expect(Object.keys(screenLoaders).sort()).toEqual(Object.keys(EXPECTED_FILE).sort());
  });

  it('loads nothing until a screen is asked for, then only that screen', async () => {
    // Importing the registry evaluated no screen module.
    expect(loaded).toEqual([]);
    expect(await load('Board')).toBe('BoardScreen');
    expect(loaded).toEqual(['BoardScreen']);
  });

  it('covers every tab and every stack route', () => {
    for (const tab of TAB_ROUTES) {
      const stack = (STACK_ROUTES as Record<string, readonly string[]>)[tab];
      const names = stack ?? [tab];
      for (const n of names) {
        expect(isLazy(n) || (EAGER_SCREENS as readonly string[]).includes(n), n).toBe(true);
      }
    }
  });
});

describe('App.tsx uses the registry', () => {
  it('registers exactly the tabs the registry lists, in order', () => {
    const tabs = [...app.matchAll(/<Tab\.Screen name="(\w+)"/g)].map((m) => m[1]);
    expect(tabs).toEqual([...TAB_ROUTES]);
  });

  it('registers each stack with the routes the registry lists', () => {
    const stacks: Record<string, string> = {
      Tasks: 'Stack',
      Apps: 'AppsStackNav',
      Security: 'SecurityStackNav',
      Docs: 'DocsStackNav',
    };
    for (const [tab, nav] of Object.entries(stacks)) {
      const re = new RegExp(`<${nav}\\.Screen\\s+name="(\\w+)"`, 'g');
      const routes = [...app.matchAll(re)].map((m) => m[1]);
      expect(routes, tab).toEqual([...(STACK_ROUTES as Record<string, readonly string[]>)[tab]]);
    }
  });

  it('builds every lazy screen from a registry name', () => {
    const names = [...app.matchAll(/lazyScreen\('(\w+)'\)/g)].map((m) => m[1]);
    expect(names.sort()).toEqual(Object.keys(screenLoaders).sort());
  });

  it('imports no other screen eagerly', () => {
    const eager = [...app.matchAll(/^import \w+ from '\.\/src\/screens\/(\w+)'/gm)].map((m) => m[1]);
    expect(eager.sort()).toEqual(['DesktopScreen', 'OnboardingScreen']);
  });
});

describe('deep links reach screens that have not loaded yet', () => {
  // The desktop app's kubecoder:// links and notification clicks, and a
  // phone push tap, all go: ref → pushTargetFromData → tapRoute → navigate.
  const cases: Array<[string, string, Partial<ReturnType<typeof tapRoute>>]> = [
    ['kubecoder://board/acme-jira/812', 'BoardScreen', { tab: 'Board' }],
    ['kubecoder://board:kube-coder-gh:I_kwDOA:4102', 'BoardScreen', { tab: 'Board' }],
    ['kubecoder://task/t-42', 'TaskDetailScreen', { tab: 'Tasks', screen: 'TaskDetail' }],
    ['kubecoder://thread/th-9', 'HypervisorScreen', { tab: 'Hypervisor' }],
    ['kubecoder://scan/sc-1', 'ScanDetailScreen', { tab: 'Security', screen: 'ScanDetail' }],
    ['kubecoder://feed', 'FeedScreen', { tab: 'Feed' }],
    ['kubecoder://nonsense/x', 'FeedScreen', { tab: 'Feed' }],
  ];

  for (const [url, file, expected] of cases) {
    it(`${url} → ${file}`, async () => {
      const route = tapRoute(pushTargetFromData({ ref: refFromDeepLink(url) }));
      expect(route).toMatchObject({ kind: 'screen', ...expected });
      if (route.kind !== 'screen') throw new Error('not a screen');
      const target = route.screen ?? firstScreenOf(route.tab);
      expect(loaded).not.toContain(file);
      expect(await load(target)).toBe(file);
      expect(loaded).toContain(file);
    });
  }

  it('keeps the routing details each screen reads', () => {
    expect(tapRoute(pushTargetFromData({ ref: 'board:acme-jira:812' }))).toEqual({
      kind: 'screen',
      tab: 'Board',
      boardFocus: { boardId: 'acme-jira', itemId: '812' },
    });
    expect(tapRoute(pushTargetFromData({ ref: 'task:t-42' }))).toEqual({
      kind: 'screen',
      tab: 'Tasks',
      screen: 'TaskDetail',
      params: { id: 't-42' },
    });
    expect(tapRoute(pushTargetFromData({ ref: 'thread:th-9' }))).toEqual({
      kind: 'screen',
      tab: 'Hypervisor',
      params: { openThreadId: 'th-9' },
    });
    expect(tapRoute(pushTargetFromData({ ref: 'memory:x' }))).toEqual({ kind: 'screen', tab: 'Memory' });
  });
});
