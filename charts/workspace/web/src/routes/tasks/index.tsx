import { useEffect } from 'preact/hooks';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { sheetOpen, drawerOpen, masterCollapsed, previewFullscreen } from '../../store/ui';
import { selectTask, selectedTaskId } from '../../store/tasks';
import { currentPath, navigate, pathSuffix } from '../../store/router';
import { TaskList } from './TaskList';
import { TaskDetail } from './TaskDetail';
import { NewTaskForm } from './NewTaskForm';
import { BottomSheet } from '../../components/BottomSheet';
import { Drawer } from '../../components/Drawer';
import { MutatorOnly } from '../../components/MutatorOnly';
import { useIsMobile } from '../../hooks/useMediaQuery';

export function TasksRoute() {
  const isMobile = useIsMobile();

  // URL → selectedTaskId. `/tasks/abc` selects task abc; `/tasks` deselects.
  // This is what makes a page reload restore the previously-open task (and the
  // TerminalPane re-attaches automatically inside TaskDetail).
  useEffect(() => {
    // Take only the first sub-segment so a hypothetical /tasks/<id>/foo
    // still selects <id> correctly.
    const suffix = pathSuffix(currentPath.value).split('/')[0];
    const target = suffix || null;
    if (target !== selectedTaskId.value) selectTask(target);
    // Also open the mobile detail sheet if landing on a deep link.
    if (target && isMobile && sheetOpen.value !== 'task-detail') {
      sheetOpen.value = 'task-detail';
    }
  }, [currentPath.value, isMobile]);
  const collapsedMaster = masterCollapsed.value;
  const fullscreen = previewFullscreen.value;
  // Compute the layout modifier as we have three states on desktop:
  //   default (split), master-collapsed (detail full), fullscreen (no header, no master).
  const layoutMod = fullscreen
    ? 'tasks-layout-fullscreen'
    : collapsedMaster
      ? 'tasks-layout-master-collapsed'
      : '';
  return (
    <div class={`route route-tasks ${fullscreen ? 'route-tasks-fullscreen' : ''}`}>
      {!fullscreen && (
        <header class="route-header route-header-with-action">
          <div>
            <h1 class="route-title">Build</h1>
            <p class="route-subtitle muted">
              Each build is a live Claude / OpenCode session in tmux. List refreshes every 10s.
            </p>
          </div>
          <MutatorOnly>
            <Button variant="primary" size="md" onClick={() => (drawerOpen.value = 'new-task')}>
              <Icon name="plus" size={14} /> New build
            </Button>
          </MutatorOnly>
        </header>
      )}

      <div class={`tasks-layout ${layoutMod}`}>
        {!isMobile && !collapsedMaster && !fullscreen && (
          <div class="tasks-master">
            <div class="tasks-master-bar">
              <span class="muted" style={{ fontSize: '11.5px' }}>Build sessions</span>
              <Button
                size="sm"
                variant="ghost"
                iconOnly
                onClick={() => (masterCollapsed.value = true)}
                aria-label="Collapse build list"
                title="Collapse build list"
              >
                <Icon name="chevron-left" size={14} />
              </Button>
            </div>
            <TaskList />
          </div>
        )}
        {isMobile && (
          <div class="tasks-master">
            <TaskList />
          </div>
        )}
        {!isMobile && (
          <div class="tasks-detail-pane">
            {collapsedMaster && !fullscreen && (
              <button
                type="button"
                class="tasks-master-restore"
                onClick={() => (masterCollapsed.value = false)}
                aria-label="Show task list"
                title="Show task list"
              >
                <Icon name="chevron-right" size={14} />
              </button>
            )}
            <TaskDetail onClose={() => navigate('/tasks')} />
          </div>
        )}
      </div>

      {/* Mobile: detail in bottom sheet. TaskDetail renders its own header,
          so we omit the sheet title to avoid a duplicate. */}
      <BottomSheet
        open={isMobile && sheetOpen.value === 'task-detail'}
        onClose={() => {
          sheetOpen.value = null;
          navigate('/tasks');
        }}
        initialSnap="full"
      >
        <TaskDetail />
      </BottomSheet>

      {/* New task: drawer on desktop, full sheet on mobile */}
      {!isMobile ? (
        <Drawer
          open={drawerOpen.value === ('new-task')}
          onClose={() => (drawerOpen.value = null)}
          title="New build"
          width={520}
        >
          <NewTaskForm onClose={() => (drawerOpen.value = null)} />
        </Drawer>
      ) : (
        <BottomSheet
          open={drawerOpen.value === ('new-task')}
          onClose={() => (drawerOpen.value = null)}
          initialSnap="full"
          title="New build"
        >
          <NewTaskForm onClose={() => (drawerOpen.value = null)} />
        </BottomSheet>
      )}
    </div>
  );
}
