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
    // Mobile detail used to ride a bottom sheet — replaced with full-
    // screen routing below. Clear any leftover sheet state so nothing
    // floats over the new fullscreen detail.
    if (isMobile && sheetOpen.value === 'task-detail') {
      sheetOpen.value = null;
    }
  }, [currentPath.value, isMobile]);
  const hasSelected = !!selectedTaskId.value;
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
        {isMobile && !hasSelected && (
          // Mobile, no task selected → full-screen build list.
          <div class="tasks-master">
            <MutatorOnly>
              <Button
                variant="primary"
                size="md"
                onClick={() => (drawerOpen.value = 'new-task')}
                class="tasks-mobile-new"
              >
                <Icon name="plus" size={14} /> New build
              </Button>
            </MutatorOnly>
            <TaskList />
          </div>
        )}
        {isMobile && hasSelected && (
          // Mobile, task selected → full-screen detail (no list, no
          // BottomSheet). TaskDetail's own header carries the back ←
          // affordance via onClose. Routes back to /tasks which clears
          // selection + this branch unmounts.
          <div class="tasks-detail-pane tasks-detail-pane-mobile">
            <TaskDetail onClose={() => navigate('/tasks')} />
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
