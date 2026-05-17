import { useEffect } from 'preact/hooks';
import {
  filteredTasks,
  selectedTaskId,
  selectTask,
  startTaskPolling,
  stopTaskPolling,
  taskFilter,
  taskStatusFilter,
  taskStatusFilterEffective,
  tasks,
  tasksError,
  tasksLastFetch,
  tasksLoading,
  taskCounts,
} from '../../store/tasks';
import { navigate } from '../../store/router';
import { sheetOpen } from '../../store/ui';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { EmptyState } from '../../components/primitives/EmptyState';
import type { TaskStatus, TaskSummary } from '../../api/tasks';
import './tasks.css';

const STATUS_TONE: Record<TaskStatus, 'success' | 'warn' | 'danger' | 'neutral' | 'accent'> = {
  running: 'accent',
  completed: 'success',
  killed: 'warn',
  error: 'danger',
  unknown: 'neutral',
  'waiting-for-input': 'warn',
};

const STATUS_LABEL: Record<TaskStatus, string> = {
  running: 'running',
  completed: 'done',
  killed: 'killed',
  error: 'error',
  unknown: 'unknown',
  'waiting-for-input': 'waiting',
};

function timeAgo(ts: number | null): string {
  if (!ts) return '—';
  const sec = Math.floor(Date.now() / 1000 - ts);
  if (sec < 5) return 'just now';
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`;
  return `${Math.floor(sec / 86400)}d`;
}

export function TaskList() {
  const isMobile = useIsMobile();
  useEffect(() => {
    startTaskPolling(10000);
    return () => stopTaskPolling();
  }, []);

  const list = filteredTasks.value;
  const counts = taskCounts.value;
  const activeId = selectedTaskId.value;
  const userChoice = taskStatusFilter.value;
  const effective = taskStatusFilterEffective.value;
  // We auto-relaxed if user picked 'running' but there are none.
  const autoRelaxed = userChoice === 'running' && effective === 'all';
  const pastCount = counts.completed + counts.error;

  function onRowClick(t: TaskSummary) {
    // URL is the source of truth so a reload restores the same selection
    // (and the TerminalPane re-attaches automatically). TasksRoute mirrors
    // currentPath → selectedTaskId; we keep selectTask here as a belt-and-
    // suspenders sync for the immediate render frame.
    navigate(`/tasks/${t.task_id}`);
    selectTask(t.task_id);
    if (isMobile) sheetOpen.value = 'task-detail';
  }

  return (
    <section class="tl">
      <div class="tl-toolbar">
        <div class="tl-seg" role="tablist" aria-label="Filter by status">
          <button
            type="button"
            role="tab"
            aria-selected={userChoice === 'running'}
            class={`tl-seg-item ${userChoice === 'running' ? 'tl-seg-item-active' : ''}`}
            onClick={() => (taskStatusFilter.value = 'running')}
            title="Show only tasks whose tmux session is alive"
          >
            Running <span class="tl-seg-count">{counts.running}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={userChoice === 'all'}
            class={`tl-seg-item ${userChoice === 'all' ? 'tl-seg-item-active' : ''}`}
            onClick={() => (taskStatusFilter.value = 'all')}
            title="Show running + past (completed, killed, errored)"
          >
            All <span class="tl-seg-count">{counts.all}</span>
          </button>
        </div>
        <Input
          fullWidth
          placeholder="Filter…"
          value={taskFilter.value}
          onInput={(e) => (taskFilter.value = (e.target as HTMLInputElement).value)}
          aria-label="Filter tasks by text"
        />
      </div>
      {autoRelaxed && (
        <div class="tl-relaxed muted" role="status">
          No running tasks — showing all {pastCount} past task{pastCount === 1 ? '' : 's'}.
        </div>
      )}

      {tasksError.value && (
        <div class="tl-error" role="alert">
          {tasksError.value}
        </div>
      )}

      {list.length === 0 ? (
        tasksLoading.value && tasks.value.length === 0 ? (
          <div class="tl-skeleton" aria-busy="true">
            {Array.from({ length: 4 }, (_, i) => (
              <div key={i} class="tl-skeleton-row" />
            ))}
          </div>
        ) : (
          <EmptyState
            icon={<Icon name="tasks" size={24} />}
            title={taskFilter.value ? 'No matches' : 'No tasks yet'}
            description={
              taskFilter.value
                ? 'Try clearing the filter or searching for something else.'
                : 'Create a task to run Claude Code in a tmux session.'
            }
          />
        )
      ) : (
        <ul class="tl-list" role="list">
          {list.map((t) => (
            <li key={t.task_id}>
              <button
                class={`tl-row ${activeId === t.task_id ? 'tl-row-active' : ''} ${t.status === 'waiting-for-input' ? 'tl-row-waiting' : ''}`}
                onClick={() => onRowClick(t)}
                aria-current={activeId === t.task_id ? 'true' : 'false'}
              >
                <div class="tl-row-head">
                  <div class="tl-row-status-container">
                    <Pill tone={STATUS_TONE[t.status]} mono>{STATUS_LABEL[t.status]}</Pill>
                    {t.status === 'waiting-for-input' && (
                      <span class="tl-waiting-indicator" title="Task is waiting for user input">
                        ⏳
                      </span>
                    )}
                  </div>
                  <span class="tl-row-title">{t.name || t.prompt || '(unnamed)'}</span>
                  <span class="tl-row-age" title={t.created_at ? new Date(t.created_at * 1000).toISOString() : ''}>
                    {timeAgo(t.created_at)}
                  </span>
                </div>
                {t.name && t.prompt && <div class="tl-row-sub muted">{t.prompt}</div>}
                {t.status === 'waiting-for-input' && t.last_input_prompt && (
                  <div class="tl-row-prompt">
                    <span class="tl-row-prompt-label">Waiting for input:</span>
                    <span class="tl-row-prompt-text">{t.last_input_prompt}</span>
                  </div>
                )}
                <div class="tl-row-meta muted">
                  <span class="mono">{t.task_id.slice(0, 18)}</span>
                  {t.source && <span> · {t.source}</span>}
                  {t.kind && t.kind !== 'claude' && <span> · {t.kind}</span>}
                  {(t.memory_injected?.length ?? 0) > 0 && (
                    <span> · {t.memory_injected!.length} mem</span>
                  )}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      {tasksLastFetch.value && (
        <div class="tl-foot muted">
          Updated {Math.round((Date.now() - tasksLastFetch.value) / 1000)}s ago · polls every 10s
        </div>
      )}
    </section>
  );
}

