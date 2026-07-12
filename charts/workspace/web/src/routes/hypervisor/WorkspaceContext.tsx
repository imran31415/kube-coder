import { Icon } from '../../components/Icon';
import { usePoll } from '../../hooks/usePoll';
import { navigate } from '../../store/router';
import {
  workspaceTasks,
  activeThreadId,
  refreshWorkspaceTasks,
} from '../../store/hypervisor';
import type { TaskSummary } from '../../api/tasks';

/**
 * A slim "workspace glance" strip above the transcript: clean chips for the
 * live entities the Hypervisor can act on — right now the other tasks/agents
 * running in the pod. Keeps the chat grounded in real state without dumping a
 * terminal. Polls independently of the thread so it stays fresh between turns.
 */

const LIVE = new Set(['running', 'waiting-for-input']);
const MAX_CHIPS = 4;

function shortTitle(t: TaskSummary): string {
  const s = (t.name || t.prompt || 'task').replace(/\s+/g, ' ').trim();
  return s.length > 26 ? s.slice(0, 25) + '…' : s;
}

export function WorkspaceContext() {
  usePoll(refreshWorkspaceTasks, 5000);

  // Everything live except this chat's own session — those are the "entities"
  // worth surfacing (builds, sub-agents, terminals).
  const live = workspaceTasks.value.filter(
    (t) => LIVE.has(t.status) && t.task_id !== activeThreadId.value,
  );
  const running = live.filter((t) => t.status === 'running').length;
  const waiting = live.filter((t) => t.status === 'waiting-for-input').length;

  const shown = live.slice(0, MAX_CHIPS);
  const overflow = live.length - shown.length;

  return (
    <div class="hv-context" role="status">
      <span class="hv-context-label">
        <Icon name="hypervisor" size={13} />
        Workspace
      </span>

      {live.length === 0 ? (
        <span class="hv-chip hv-chip-idle">
          <span class="hv-chip-dot" /> Idle · no tasks running
        </span>
      ) : (
        <>
          <span class="hv-context-summary">
            {running > 0 && `${running} running`}
            {running > 0 && waiting > 0 && ' · '}
            {waiting > 0 && `${waiting} need you`}
          </span>
          <div class="hv-chip-row">
            {shown.map((t) => (
              <button
                key={t.task_id}
                type="button"
                class={`hv-chip hv-chip-task hv-chip-${t.status}`}
                title={`Open “${shortTitle(t)}” in Build`}
                onClick={() => navigate(`/tasks/${t.task_id}`)}
              >
                <span class="hv-chip-dot" />
                <Icon name="tasks" size={12} />
                <span class="hv-chip-text">{shortTitle(t)}</span>
              </button>
            ))}
            {overflow > 0 && (
              <button
                type="button"
                class="hv-chip hv-chip-more"
                title="View all tasks"
                onClick={() => navigate('/tasks')}
              >
                +{overflow} more
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
