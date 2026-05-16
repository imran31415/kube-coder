import type { FunctionComponent } from 'preact';
import { useEffect } from 'preact/hooks';
import { currentPath, matchRoute, ROUTES } from '../store/router';
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
import { MemoryRoute } from './memory/index';
import { TriggersRoute } from './triggers/index';
import { FilesRoute } from './files/index';
import { SettingsRoute } from './settings/index';
import { MoreSheet } from './more/index';
import './Shell.css';

const ROUTE_COMPONENTS: Record<string, FunctionComponent> = {
  '/tasks': TasksRoute,
  '/memory': MemoryRoute,
  '/triggers': TriggersRoute,
  '/files': FilesRoute,
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

  // Set the document title from the active route.
  useEffect(() => {
    document.title = `${route.title} · kube-coder`;
  }, [route.title]);

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
      >
        <MoreSheet />
      </BottomSheet>
    </div>
  );
}

// Re-export so TS recognises the available routes in this file.
export { ROUTES };
