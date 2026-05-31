import type { FunctionComponent } from 'preact';
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
import { TasksRoute } from './tasks/index';
import { DesktopRoute } from './desktop/index';
import { MemoryRoute } from './memory/index';
import { TriggersRoute } from './triggers/index';
import { FilesRoute } from './files/index';
import { SettingsRoute } from './settings/index';
import { DocsRoute } from './docs/index';
import { AppsRoute } from './apps/index';
import { MoreSheet } from './more/index';
import { waitingTasks } from '../components/WaitingBadge';
import './Shell.css';

const ROUTE_COMPONENTS: Record<string, FunctionComponent> = {
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
  const RouteComponent: FunctionComponent = ROUTE_COMPONENTS[route.path] ?? ROUTE_COMPONENTS['/tasks'];

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
          <RouteComponent />
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
