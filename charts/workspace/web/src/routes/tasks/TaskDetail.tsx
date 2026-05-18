import { useEffect, useState } from 'preact/hooks';
import './detail.css';
import {
  selectedTask,
  selectedTaskLoading,
  killTask,
  renameTask,
  selectTask,
} from '../../store/tasks';
import { listSubagents } from '../../api/subagents';
import { Pill } from '../../components/primitives/Pill';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { TerminalPane } from './TerminalPane';
import { SubagentsTab } from './SubagentsTab';
import { MessageChat } from './MessageChat';
import { EmptyState } from '../../components/primitives/EmptyState';
import { ConfirmDialog, PromptDialog } from '../../components/ConfirmDialog';
import { MutatorOnly } from '../../components/MutatorOnly';
import type { TaskStatus } from '../../api/tasks';

const STATUS_TONE: Record<TaskStatus, 'success' | 'warn' | 'danger' | 'neutral' | 'accent'> = {
  running: 'accent',
  completed: 'success',
  killed: 'warn',
  error: 'danger',
  unknown: 'neutral',
  'waiting-for-input': 'warn',
};

// Short labels for the pill — the raw status "waiting-for-input" is 17 chars
// and overflows the header on narrow screens (clips Copy link / Rename).
const STATUS_PILL_LABEL: Record<TaskStatus, string> = {
  running: 'running',
  completed: 'done',
  killed: 'killed',
  error: 'error',
  unknown: 'unknown',
  'waiting-for-input': 'waiting',
};

const STATUS_HELP: Record<TaskStatus, string> = {
  running: 'Task is alive in tmux. Output streams in the Session tab.',
  completed: 'Task exited cleanly. Output is preserved; tmux session may have been reaped.',
  killed: 'Task was killed via the dashboard.',
  error: 'Task exited with an error code.',
  unknown: 'Status could not be determined.',
  'waiting-for-input': 'Task is waiting for user input. Check the Session or Send message tab to respond.',
};

type DetailTab = 'terminal' | 'preview' | 'message' | 'info' | 'subagents';
// The "terminal" id is historical — what the user sees is "Session", which is
// the live attach to the task's tmux/Claude session. terminal-entry.sh falls
// back to the most-recent claude-* session when the pending file is missing,
// so the tab always lands the user inside their build.
const TAB_LABELS: Record<DetailTab, string> = {
  terminal: 'Session',
  preview: 'Preview',
  message: 'Send message',
  info: 'Info',
  subagents: 'Subagents',
};
const TAB_HELP: Record<DetailTab, string> = {
  terminal: 'Live Claude/OpenCode session — attach and type as if you were SSH\'d into the pod.',
  preview: 'Side-by-side session + workspace browser (noVNC).',
  message: 'Chat-style composer that mirrors the session in a friendly UI.',
  info: 'Metadata, prompt, timestamps, and injected memory.',
  subagents: 'Sub-tasks spawned by Claude\'s Agent / Task tool.',
};

/**
 * A task is "alive" — meaning its tmux session still exists and the user
 * can interact with it — when it's either actively running OR paused
 * waiting for human input. Both states need the Terminal/Preview/Message
 * tabs (the user typically wants to respond to the prompt).
 */
function isAliveStatus(s: TaskStatus | undefined): boolean {
  return s === 'running' || s === 'waiting-for-input';
}

export function TaskDetail({ onClose }: { onClose?: () => void }) {
  const t = selectedTask.value;
  const isLive = isAliveStatus(t?.status);
  // Default tab depends on whether the task is alive — past tasks default to
  // Info because there's no tmux session to attach a terminal to.
  const [tab, setTab] = useState<DetailTab>(isLive ? 'terminal' : 'info');
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [subagentsCount, setSubagentsCount] = useState<number>(0);
  const [confirmKill, setConfirmKill] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  // Lets the user reclaim vertical space (mobile especially) by collapsing
  // the task header + meta strip. Tabs stay visible since they're the
  // primary nav inside the detail view.
  const [headerCollapsed, setHeaderCollapsed] = useState(false);

  // When the selected task CHANGES, snap to a sensible default tab:
  //   - live (running)  → Terminal (the user wants to watch it work)
  //   - finished        → Info (interactive tabs are hidden anyway)
  // When only the status flips on the SAME task (running → completed), bump
  // the user off interactive tabs since the tmux session is gone.
  useEffect(() => {
    if (!t) return;
    setTab(isAliveStatus(t.status) ? 'terminal' : 'info');
    // Reset only on task-id change so user keystrokes inside the same task
    // (status flips, etc.) don't yank them off the tab they're reading.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t?.task_id]);

  useEffect(() => {
    if (!t) return;
    const live = isAliveStatus(t.status);
    if (!live && (tab === 'terminal' || tab === 'preview' || tab === 'message')) {
      setTab('info');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t?.status]);

  // Poll subagents count so we can hide the tab when empty.
  const sessionId = t && typeof t.session_id === 'string' ? t.session_id : undefined;
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await listSubagents();
        if (cancelled) return;
        const filtered = sessionId ? r.subagents.filter((s) => s.session_id === sessionId) : r.subagents;
        setSubagentsCount(filtered.length);
      } catch {
        if (cancelled) return;
        setSubagentsCount(0);
      }
    }
    if (!t) {
      setSubagentsCount(0);
      return;
    }
    void tick();
    const id = window.setInterval(tick, 20000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [t?.task_id, sessionId]);

  // If user is on the Subagents tab and the count drops to zero, kick them
  // back to Terminal so they don't see an empty/hidden tab content.
  useEffect(() => {
    if (tab === 'subagents' && subagentsCount === 0) setTab('terminal');
  }, [tab, subagentsCount]);

  if (!t) {
    if (selectedTaskLoading.value) {
      return <div class="td-empty muted">Loading…</div>;
    }
    return (
      <EmptyState
        icon={<Icon name="tasks" size={24} />}
        title="Select a task"
        description="Pick a task from the list to see its live output, attach a terminal, or send a follow-up."
      />
    );
  }

  function onKill() {
    setConfirmKill(true);
  }
  async function confirmKillNow() {
    setConfirmKill(false);
    setBusy(true);
    if (t) await killTask(t.task_id);
    setBusy(false);
  }
  function onRename() {
    setRenameOpen(true);
  }
  async function confirmRename(name: string) {
    setRenameOpen(false);
    if (!t || !name) return;
    setBusy(true);
    await renameTask(t.task_id, name);
    setBusy(false);
  }
  async function onCopyLink() {
    if (!t) return;
    // Deep-link path — TasksRoute parses /tasks/<id> on load and re-attaches
    // the terminal automatically. Drops the old `?id=` query form that nothing
    // parsed (see planning doc, frontend obs. #2).
    const url = `${window.location.origin}/tasks/${encodeURIComponent(t.task_id)}`;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  }

  // For finished tasks (completed/killed/error/unknown) the tmux session is
  // gone — hide the interactive tabs so the user doesn't try to attach.
  const visibleTabs: DetailTab[] = isLive
    ? ['terminal', 'preview', 'message', 'info']
    : ['info'];
  if (subagentsCount > 0) visibleTabs.push('subagents');

  const FINISHED_BANNER: Partial<Record<TaskStatus, { tone: 'success' | 'warn' | 'danger' | 'neutral'; title: string; body: string }>> = {
    completed: {
      tone: 'success',
      title: 'Completed',
      body: 'This task finished cleanly. The tmux session has been reaped, so you can no longer attach a terminal or send follow-up messages. Output and subagent history are preserved below.',
    },
    killed: {
      tone: 'warn',
      title: 'Killed',
      body: 'This task was stopped via the dashboard. The tmux session is no longer running. Output and subagent history are preserved below.',
    },
    error: {
      tone: 'danger',
      title: 'Errored',
      body: 'This task exited with an error. Check the Subagents tab and output history for context.',
    },
    unknown: {
      tone: 'neutral',
      title: 'Status unknown',
      body: 'Could not determine the task status. The tmux session may have been lost.',
    },
  };
  const banner = !isLive ? FINISHED_BANNER[t.status] : undefined;

  return (
    <article class={`td ${headerCollapsed ? 'td-collapsed' : ''}`}>
      {!headerCollapsed && (
        <header class="td-header">
          <div class="td-headline">
            <Pill tone={STATUS_TONE[t.status]} mono title={STATUS_HELP[t.status]}>
              {STATUS_PILL_LABEL[t.status]}
            </Pill>
            <h2 class="td-title" title={t.prompt || undefined}>
              {t.name || t.prompt || '(unnamed)'}
            </h2>
          </div>
          <div class="td-actions">
            <Button
              size="sm"
              variant="ghost"
              onClick={onCopyLink}
              disabled={busy}
              title="Copy a deep-link URL to this task to your clipboard"
            >
              {copied ? 'Copied' : 'Copy link'}
            </Button>
            <MutatorOnly>
              <Button
                size="sm"
                variant="ghost"
                onClick={onRename}
                disabled={busy}
                title="Rename this task (display only — doesn't affect the tmux session)"
              >
                Rename
              </Button>
              <Button
                size="sm"
                variant={isLive ? 'danger' : 'ghost'}
                onClick={onKill}
                disabled={busy || !isLive}
                title={
                  isLive
                    ? 'Kill the tmux session (output is preserved)'
                    : 'Already stopped'
                }
              >
                <Icon name="kill" size={14} /> Kill
              </Button>
            </MutatorOnly>
            <Button
              size="sm"
              variant="ghost"
              iconOnly
              onClick={() => setHeaderCollapsed(true)}
              aria-label="Hide task header"
              title="Hide the header + meta strip — gives the terminal more vertical space"
            >
              <Icon name="chevron-down" size={14} />
            </Button>
            {onClose && (
              <Button
                size="sm"
                variant="ghost"
                iconOnly
                onClick={onClose}
                aria-label="Close detail"
                title="Deselect task"
              >
                <Icon name="close" />
              </Button>
            )}
          </div>
        </header>
      )}
      {headerCollapsed && (
        <button
          type="button"
          class="td-expand"
          onClick={() => setHeaderCollapsed(false)}
          aria-label="Show task header"
          title="Show the task header again"
        >
          <Icon name="chevron-right" size={12} />
          <span class="mono">{t.name || t.task_id.slice(0, 12)}</span>
          <Pill tone={STATUS_TONE[t.status]} mono>{STATUS_PILL_LABEL[t.status]}</Pill>
        </button>
      )}

      {!headerCollapsed && (
      <div class="td-meta">
        {t.workdir && (
          <span
            class="td-meta-path"
            title="Working directory inside the workspace pod (cwd of the tmux session)"
          >
            <Icon name="files" size={11} />
            <span class="mono">{String(t.workdir)}</span>
          </span>
        )}
        <span class="td-meta-chip mono muted" title="Internal task ID — used by /api/claude/tasks endpoints">
          {t.task_id}
        </span>
        {t.assistant && (
          <span class="td-meta-chip muted" title="Which assistant is driving this task">
            {String(t.assistant)}
          </span>
        )}
        {t.source && (
          <span class="td-meta-chip muted" title="Where the task originated (dashboard, MCP, cron, webhook, …)">
            {t.source}
          </span>
        )}
      </div>
      )}

      <nav class="td-tabs" role="tablist">
        {visibleTabs.map((id) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            class={`td-tab ${tab === id ? 'td-tab-active' : ''}`}
            onClick={() => setTab(id)}
            title={TAB_HELP[id]}
          >
            {TAB_LABELS[id]}
            {id === 'subagents' && subagentsCount > 0 && (
              <span class="td-tab-count" aria-label={`${subagentsCount} subagent invocations`}>
                {subagentsCount}
              </span>
            )}
          </button>
        ))}
      </nav>

      <div class="td-body" role="tabpanel">
        {banner && (
          <div class={`td-banner td-banner-${banner.tone}`} role="status">
            <strong>{banner.title}</strong>
            <span>{banner.body}</span>
          </div>
        )}
        {tab === 'terminal' && <TerminalPane taskId={t.task_id} />}
        {tab === 'preview' && <TerminalPane taskId={t.task_id} withVnc />}
        {tab === 'message' && <MessageChat taskId={t.task_id} status={t.status} taskName={t.name} />}
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
                <dt title="Attach via `tmux attach -t <name>` inside the pod">tmux session</dt>
                <dd class="mono">{String(t.tmux_session)}</dd>
              </>
            )}
            {(t.memory_injected?.length ?? 0) > 0 && (
              <>
                <dt title="Memory records spliced into the prompt at task creation">Memory injected</dt>
                <dd>{t.memory_injected!.map((m) => `${m.namespace}.${m.key}`).join(', ')}</dd>
              </>
            )}
          </dl>
        )}
        {tab === 'subagents' && (
          <SubagentsTab sessionId={sessionId} />
        )}
      </div>
      <ConfirmDialog
        open={confirmKill}
        title="Kill this task?"
        body="The tmux session will be terminated. Output and history are preserved — you just can't send any more messages or attach a terminal."
        confirmLabel="Kill task"
        destructive
        onConfirm={confirmKillNow}
        onCancel={() => setConfirmKill(false)}
      />
      <PromptDialog
        open={renameOpen}
        title="Rename task"
        body="Pick a short, memorable name. The original task_id never changes."
        initial={t?.name ?? ''}
        placeholder="e.g. PR review #42"
        confirmLabel="Save name"
        onConfirm={confirmRename}
        onCancel={() => setRenameOpen(false)}
      />
    </article>
  );
}

// Convenience for mobile sheet — close-and-deselect button.
export function deselectAndClose(setSheet: (v: null) => void) {
  selectTask(null);
  setSheet(null);
}
