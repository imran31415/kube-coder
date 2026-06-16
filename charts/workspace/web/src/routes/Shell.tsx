import type { ComponentType } from 'preact';
import { lazy, Suspense } from 'preact/compat';
import { useEffect } from 'preact/hooks';
import { currentPath, matchRoute } from '../store/router';
import { paletteOpen, sheetOpen } from '../store/ui';
import { useShortcut } from '../hooks/useShortcut';
import { Topbar } from '../components/Topbar';
import { Rail } from '../components/Rail';
import { BottomNav } from '../components/BottomNav';
import { CommandPalette } from '../components/CommandPalette';
import { ToastRack } from '../components/Toast';
import { BottomSheet } from '../components/BottomSheet';
import { ShortcutsHelp } from '../components/ShortcutsHelp';
import { Onboarding } from '../components/Onboarding';
import { MoreSheet } from './more/index';
import { waitingTasks } from '../components/WaitingBadge';
import './Shell.css';

// Routes are code-split: each becomes its own chunk fetched on first visit, so
// route-only heavy deps (D3 in Memory, marked/DOMPurify in Docs) stay out of
// the initial bundle. `MoreSheet` stays eager — it's tiny and lives inside the
// always-mounted BottomSheet. See issue #101.
const TasksRoute = lazy<ComponentType>(() => import('./tasks/index').then((m) => ({ default: m.TasksRoute })));
const DesktopRoute = lazy<ComponentType>(() => import('./desktop/index').then((m) => ({ default: m.DesktopRoute })));
const MemoryRoute = lazy<ComponentType>(() => import('./memory/index').then((m) => ({ default: m.MemoryRoute })));
const TriggersRoute = lazy<ComponentType>(() => import('./triggers/index').then((m) => ({ default: m.TriggersRoute })));
const FilesRoute = lazy<ComponentType>(() => import('./files/index').then((m) => ({ default: m.FilesRoute })));
const SettingsRoute = lazy<ComponentType>(() => import('./settings/index').then((m) => ({ default: m.SettingsRoute })));
const DocsRoute = lazy<ComponentType>(() => import('./docs/index').then((m) => ({ default: m.DocsRoute })));
const AppsRoute = lazy<ComponentType>(() => import('./apps/index').then((m) => ({ default: m.AppsRoute })));

const ROUTE_COMPONENTS: Record<string, ComponentType> = {
  '/tasks': TasksRoute,
  '/desktop': DesktopRoute,
  '/memory': MemoryRoute,
  '/apps': AppsRoute,
  '/triggers': TriggersRoute,
  '/files': FilesRoute,
  '/docs': DocsRoute,
  '/settings': SettingsRoute,
};

export function Shell() {
  const route = matchRoute(currentPath.value);
  const RouteComponent: ComponentType = ROUTE_COMPONENTS[route.path] ?? ROUTE_COMPONENTS['/tasks'];

  // Cmd-K / Ctrl-K toggles the palette globally.
  useShortcut({ key: 'k', meta: true, allowInInput: true }, (e) => {
    e.preventDefault();
    paletteOpen.value = !paletteOpen.value;
  });
  // `?` is wired inside ShortcutsHelp itself.

  // Set the document title from the active route AND prepend a (N) prefix
  // when tasks are paused waiting for human input. Tab-bar visibility of the
  // waiting count is the biggest single UX win for async Claude workflows —
  // users routinely walk away while a task chews, and the title is the only
  // surface that survives a backgrounded tab.
  const waitingCount = waitingTasks.value.length;
  useEffect(() => {
    const base = `${route.title} · kube-coder`;
    document.title = waitingCount > 0 ? `(${waitingCount}) ${base}` : base;
  }, [route.title, waitingCount]);

  return (
    <div class="app-shell" data-active-route={route.path}>
      <Topbar />
      <div class="app-body">
        <Rail />
        <main class="app-main" tabIndex={-1}>
          <Suspense fallback={<div class="route-loading" aria-busy="true" aria-label="Loading…" />}>
            <RouteComponent />
          </Suspense>
        </main>
      </div>
      <BottomNav />
      <CommandPalette />
      <ShortcutsHelp />
      <Onboarding />
      <ToastRack />
      <BottomSheet
        open={sheetOpen.value === 'more'}
        onClose={() => (sheetOpen.value = null)}
        title="More"
        initialSnap="full"
      >
        <MoreSheet />
      </BottomSheet>
    </div>
  );
}
