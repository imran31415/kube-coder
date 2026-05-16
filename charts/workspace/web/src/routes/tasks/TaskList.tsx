import { useEffect } from 'preact/hooks';
import {
  filteredTasks,
  selectedTaskId,
  selectTask,
  startTaskPolling,
  stopTaskPolling,
  taskFilter,
  tasks,
  tasksError,
  tasksLastFetch,
  tasksLoading,
  taskCounts,
} from '../../store/tasks';
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
};

const STATUS_LABEL: Record<TaskStatus, string> = {
  running: 'running',
  completed: 'done',
  killed: 'killed',
  error: 'error',
  unknown: 'unknown',
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

  function onRowClick(t: TaskSummary) {
    selectTask(t.task_id);
    if (isMobile) sheetOpen.value = 'task-detail';
  }

  return (
    <section class="tl">
      <div class="tl-toolbar">
        <Input
          fullWidth
          placeholder="Filter by name, prompt, or status…"
          value={taskFilter.value}
          onInput={(e) => (taskFilter.value = (e.target as HTMLInputElement).value)}
          aria-label="Filter tasks"
        />
        <div class="tl-counts" aria-label="Task counts">
          <Pill tone="accent" mono>{counts.running} running</Pill>
          <Pill tone="success" mono>{counts.completed} done</Pill>
          {counts.error > 0 && <Pill tone="danger" mono>{counts.error} failed</Pill>}
        </div>
      </div>

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
                class={`tl-row ${activeId === t.task_id ? 'tl-row-active' : ''}`}
                onClick={() => onRowClick(t)}
                aria-current={activeId === t.task_id ? 'true' : 'false'}
              >
                <div class="tl-row-head">
                  <Pill tone={STATUS_TONE[t.status]} mono>{STATUS_LABEL[t.status]}</Pill>
                  <span class="tl-row-title">{t.name || t.prompt || '(unnamed)'}</span>
                  <span class="tl-row-age" title={t.created_at ? new Date(t.created_at * 1000).toISOString() : ''}>
                    {timeAgo(t.created_at)}
                  </span>
                </div>
                {t.name && t.prompt && <div class="tl-row-sub muted">{t.prompt}</div>}
                <div class="tl-row-meta muted">
                  <span class="mono">{t.task_id.slice(0, 18)}</span>
                  {t.source && <span> · {t.source}</span>}
                  {t.kind && t.kind !== 'claude' && <span> · {t.kind}</span>}
                  {t.memory_injected.length > 0 && (
                    <span> · {t.memory_injected.length} mem</span>
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

