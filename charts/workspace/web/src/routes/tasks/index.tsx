import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { sheetOpen, drawerOpen } from '../../store/ui';
import { selectTask } from '../../store/tasks';
import { TaskList } from './TaskList';
import { TaskDetail } from './TaskDetail';
import { NewTaskForm } from './NewTaskForm';
import { BottomSheet } from '../../components/BottomSheet';
import { Drawer } from '../../components/Drawer';
import { useIsMobile } from '../../hooks/useMediaQuery';

export function TasksRoute() {
  const isMobile = useIsMobile();
  return (
    <div class="route route-tasks">
      <header class="route-header route-header-with-action">
        <div>
          <h1 class="route-title">Tasks</h1>
          <p class="route-subtitle muted">
            Claude Code runs in tmux sessions. List updates every 10 seconds; output streams live over SSE.
          </p>
        </div>
        <Button variant="primary" size="md" onClick={() => (drawerOpen.value = 'new-task')}>
          <Icon name="plus" size={14} /> New task
        </Button>
      </header>

      <div class="tasks-layout">
        <div class="tasks-master">
          <TaskList />
        </div>
        {!isMobile && (
          <div class="tasks-detail-pane">
            <TaskDetail onClose={() => selectTask(null)} />
          </div>
        )}
      </div>

      {/* Mobile: detail in bottom sheet. TaskDetail renders its own header,
          so we omit the sheet title to avoid a duplicate. */}
      <BottomSheet
        open={isMobile && sheetOpen.value === 'task-detail'}
        onClose={() => {
          sheetOpen.value = null;
          selectTask(null);
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
          title="New task"
          width={520}
        >
          <NewTaskForm onClose={() => (drawerOpen.value = null)} />
        </Drawer>
      ) : (
        <BottomSheet
          open={drawerOpen.value === ('new-task')}
          onClose={() => (drawerOpen.value = null)}
          initialSnap="full"
          title="New task"
        >
          <NewTaskForm onClose={() => (drawerOpen.value = null)} />
        </BottomSheet>
      )}
    </div>
  );
}
