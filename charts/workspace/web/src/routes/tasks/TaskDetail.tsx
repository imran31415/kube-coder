import { useState } from 'preact/hooks';
import {
  selectedTask,
  selectedTaskLoading,
  killTask,
  renameTask,
  sendFollowup,
  selectTask,
} from '../../store/tasks';
import { Pill } from '../../components/primitives/Pill';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { TaskOutput } from './TaskOutput';
import { SubagentsTab } from './SubagentsTab';
import { EmptyState } from '../../components/primitives/EmptyState';
import type { TaskStatus } from '../../api/tasks';

const STATUS_TONE: Record<TaskStatus, 'success' | 'warn' | 'danger' | 'neutral' | 'accent'> = {
  running: 'accent',
  completed: 'success',
  killed: 'warn',
  error: 'danger',
  unknown: 'neutral',
};

type DetailTab = 'output' | 'info' | 'subagents' | 'message';

export function TaskDetail({ onClose }: { onClose?: () => void }) {
  const t = selectedTask.value;
  const [tab, setTab] = useState<DetailTab>('output');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  if (!t) {
    if (selectedTaskLoading.value) {
      return <div class="td-empty muted">Loading…</div>;
    }
    return (
      <EmptyState
        icon={<Icon name="tasks" size={24} />}
        title="Select a task"
        description="Pick a task from the list to see its live output, info, and follow-up controls."
      />
    );
  }

  async function onKill() {
    if (!confirm('Kill this task? Output is preserved.')) return;
    setBusy(true);
    if (t) await killTask(t.task_id);
    setBusy(false);
  }
  async function onRename() {
    const name = prompt('New name:', t?.name ?? '');
    if (name == null || !t) return;
    setBusy(true);
    await renameTask(t.task_id, name);
    setBusy(false);
  }
  async function onSend(e: Event) {
    e.preventDefault();
    if (!msg.trim() || !t) return;
    setBusy(true);
    await sendFollowup(t.task_id, msg.trim());
    setMsg('');
    setBusy(false);
  }

  return (
    <article class="td">
      <header class="td-header">
        <div class="td-headline">
          <Pill tone={STATUS_TONE[t.status]} mono>{t.status}</Pill>
          <h2 class="td-title">{t.name || t.prompt || '(unnamed)'}</h2>
        </div>
        <div class="td-actions">
          <Button size="sm" variant="ghost" onClick={onRename} disabled={busy}>
            Rename
          </Button>
          <Button
            size="sm"
            variant={t.status === 'running' ? 'danger' : 'ghost'}
            onClick={onKill}
            disabled={busy || t.status !== 'running'}
          >
            <Icon name="kill" size={14} /> Kill
          </Button>
          {onClose && (
            <Button size="sm" variant="ghost" iconOnly onClick={onClose} aria-label="Close detail">
              <Icon name="close" />
            </Button>
          )}
        </div>
      </header>

      <div class="td-meta muted">
        <span class="mono">{t.task_id}</span>
        {t.workdir && <span> · {String(t.workdir)}</span>}
        {t.assistant && <span> · assistant {String(t.assistant)}</span>}
        {t.source && <span> · source {t.source}</span>}
      </div>

      <nav class="td-tabs" role="tablist">
        {(['output', 'info', 'subagents', 'message'] as DetailTab[]).map((id) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            class={`td-tab ${tab === id ? 'td-tab-active' : ''}`}
            onClick={() => setTab(id)}
          >
            {id === 'output' ? 'Output' : id === 'info' ? 'Info' : id === 'subagents' ? 'Subagents' : 'Send message'}
          </button>
        ))}
      </nav>

      <div class="td-body" role="tabpanel">
        {tab === 'output' && <TaskOutput taskId={t.task_id} live={t.status === 'running'} />}
        {tab === 'info' && (
          <dl class="td-info">
            {t.prompt && (
              <>
                <dt>Prompt</dt>
                <dd>{t.prompt}</dd>
              </>
            )}
            {t.created_at && (
              <>
                <dt>Created</dt>
                <dd>{new Date(t.created_at * 1000).toLocaleString()}</dd>
              </>
            )}
            {t.finished_at && (
              <>
                <dt>Finished</dt>
                <dd>{new Date(t.finished_at * 1000).toLocaleString()}</dd>
              </>
            )}
            {t.tmux_session && (
              <>
                <dt>tmux session</dt>
                <dd class="mono">{String(t.tmux_session)}</dd>
              </>
            )}
            {t.memory_injected.length > 0 && (
              <>
                <dt>Memory injected</dt>
                <dd>{t.memory_injected.map((m) => `${m.namespace}.${m.key}`).join(', ')}</dd>
              </>
            )}
          </dl>
        )}
        {tab === 'subagents' && (
          <SubagentsTab sessionId={typeof t.session_id === 'string' ? t.session_id : undefined} />
        )}
        {tab === 'message' && (
          <form class="td-msg" onSubmit={onSend}>
            <textarea
              class="td-msg-input"
              placeholder="Continue the conversation…"
              value={msg}
              onInput={(e) => setMsg((e.target as HTMLTextAreaElement).value)}
              rows={4}
              disabled={busy}
            />
            <div class="td-msg-actions">
              <span class="muted">Sent to the same tmux session as a follow-up prompt.</span>
              <Button type="submit" variant="primary" disabled={busy || !msg.trim()}>
                Send
              </Button>
            </div>
          </form>
        )}
      </div>
    </article>
  );
}

// Convenience for mobile sheet — close-and-deselect button.
export function deselectAndClose(setSheet: (v: null) => void) {
  selectTask(null);
  setSheet(null);
}
