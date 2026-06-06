import { useEffect, useState } from 'preact/hooks';
import { listSubagents, type SubagentInvocation } from '../../api/subagents';
import { Pill } from '../../components/primitives/Pill';
import './subagents.css';

const STATUS_TONE: Record<string, 'success' | 'warn' | 'danger' | 'accent'> = {
  running: 'accent',
  'waiting-for-input': 'accent',
  completed: 'success',
  error: 'danger',
  killed: 'warn',
};

export function SubagentsTab({ taskId }: { taskId?: string }) {
  const [list, setList] = useState<SubagentInvocation[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await listSubagents(taskId);
        if (cancelled) return;
        setList(r.subagents);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load');
      }
    }
    void tick();
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void tick();
    }, 15000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [taskId]);

  if (error) {
    return <div class="sa-error">{error}</div>;
  }
  if (list.length === 0) {
    return (
      <div class="sa-empty muted">
        {taskId
          ? 'No spawned sub-agents for this task. Use the agent-orchestrator MCP tools to spawn parallel agents.'
          : 'No task selected. Open a task to see its spawned sub-agents.'}
      </div>
    );
  }

  return (
    <ul class="sa-list" role="list">
      {list.map((s) => (
        <li key={s.tool_use_id} class="sa-row">
          <div class="sa-row-head">
            <Pill tone={STATUS_TONE[s.status] ?? 'neutral'} mono>{s.status}</Pill>
            <span class="sa-row-type mono">{s.subagent_type ?? s.tool}</span>
            <span class="sa-row-time mono muted">
              {s.timestamp ? new Date(s.timestamp).toLocaleTimeString() : ''}
              {s.ended_at && ` \u2192 ${new Date(s.ended_at).toLocaleTimeString()}`}
            </span>
          </div>
          {s.description && <div class="sa-row-desc">{s.description}</div>}
          {s.prompt && (
            <details class="sa-row-prompt">
              <summary class="muted">View prompt ({s.prompt.length} chars)</summary>
              <pre class="mono">{s.prompt}</pre>
            </details>
          )}
        </li>
      ))}
    </ul>
  );
}