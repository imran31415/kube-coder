import { useEffect, useRef, useState } from 'preact/hooks';
import './detail.css';
import type { TaskDetail as TaskDetailType } from '../../api/tasks';
import { PopoverMenu, PopoverItem, PopoverSection, PopoverDivider } from '../../components/PopoverMenu';
import { previewFullscreen } from '../../store/ui';
import { getSessionSignals } from './sessionSignals';
import {
  openTerminalInNewTab,
  toggleScrollMode,
  triggerReattach,
  uploadFileToTask,
} from './sessionActions';
import {
  selectedTask,
  selectedTaskLoading,
  killTask,
  renameTask,
} from '../../store/tasks';
import { listSubagents } from '../../api/subagents';
import { Icon } from '../../components/Icon';
import { TerminalPane } from './TerminalPane';
import { SubagentsTab } from './SubagentsTab';
import { MessageChat } from './MessageChat';
import { EmptyState } from '../../components/primitives/EmptyState';
import { ConfirmDialog, PromptDialog } from '../../components/ConfirmDialog';
import { MutatorOnly } from '../../components/MutatorOnly';
import type { TaskStatus } from '../../api/tasks';

// STATUS_TONE / STATUS_PILL_LABEL / STATUS_HELP previously drove the
// task header's status pill. The header was retired when the bars were
// consolidated into TaskBar; status is now communicated by the colored
// dot at the left of the bar (see TaskBar's td-bar-dot rules). Keeping
// TAB_HELP / TAB_LABELS only since those still feed the tab buttons.

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
  preview: 'Side-by-side session + app preview — in-app iframe or in-pod browser (noVNC).',
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
  // (headerCollapsed state removed — the prior collapsible header was
  // retired in the bar-consolidation pass; TaskBar is now always
  // visible and carries everything.)

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
    const id = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void tick();
    }, 20000);
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

  const isSessionTab = tab === 'terminal' || tab === 'preview';
  return (
    <article class="td">
      <TaskBar
        task={t}
        tab={tab}
        setTab={setTab}
        visibleTabs={visibleTabs}
        subagentsCount={subagentsCount}
        isLive={isLive}
        isSessionTab={isSessionTab}
        copied={copied}
        busy={busy}
        onCopyLink={onCopyLink}
        onRename={onRename}
        onKill={onKill}
        onClose={onClose}
      />

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

/** Skinny unified bar replacing the prior 3 stacked rows (task header,
 *  meta strip, term-pane action bar). Holds: status dot, scroll-mode
 *  indicator, tabs, fullscreen toggle (Preview only), and a single
 *  Settings menu that absorbs every per-task action. */
function TaskBar({
  task, tab, setTab, visibleTabs, subagentsCount, isLive, isSessionTab,
  copied, busy, onCopyLink, onRename, onKill, onClose,
}: {
  task: TaskDetailType;
  tab: DetailTab;
  setTab: (t: DetailTab) => void;
  visibleTabs: DetailTab[];
  subagentsCount: number;
  isLive: boolean;
  isSessionTab: boolean;
  copied: boolean;
  busy: boolean;
  onCopyLink: () => void;
  onRename: () => void;
  onKill: () => void;
  onClose?: () => void;
}) {
  const s = getSessionSignals(task.task_id);
  const uploadRef = useRef<HTMLInputElement | null>(null);
  // Hidden file input — clicking the menu item just synthesizes a click
  // on the input so we get the native picker without restructuring the
  // popover (popovers don't host functional inputs cleanly).
  function pickFile() {
    uploadRef.current?.click();
  }
  function onFileChosen(e: Event) {
    const input = e.target as HTMLInputElement;
    const f = input.files?.[0];
    if (f) void uploadFileToTask(task.task_id, f);
    input.value = '';
  }
  const phase = s.phase.value;
  const scrollOn = s.scrollMode.value;

  return (
    <div class="td-bar" role="toolbar" aria-label="Task controls">
      {/* Status dot — color reflects TerminalPane phase. Click to reattach. */}
      <button
        type="button"
        class={`td-bar-dot td-bar-dot-${phase}`}
        title={
          phase === 'ready' ? 'Attached — click to reattach'
          : phase === 'preparing' ? 'Preparing session…'
          : 'Error attaching — click to retry'
        }
        aria-label={`Session ${phase}, click to reattach`}
        onClick={() => triggerReattach(task.task_id)}
        disabled={!isSessionTab}
      />
      {scrollOn && (
        <span class="td-bar-scroll mono" title="Scroll mode active — click in the menu to exit">
          SCROLL
        </span>
      )}
      <nav class="td-bar-tabs" role="tablist">
        {visibleTabs.map((id) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            class={`td-bar-tab ${tab === id ? 'td-bar-tab-active' : ''}`}
            onClick={() => setTab(id)}
            title={TAB_HELP[id]}
          >
            {TAB_LABELS[id]}
            {id === 'subagents' && subagentsCount > 0 && (
              <span class="td-bar-tab-count">{subagentsCount}</span>
            )}
          </button>
        ))}
      </nav>
      <span class="td-bar-grow" />
      {/* Hidden file input must live in the DOM near the trigger so
          synthetic click works. Outside the popover panel so its lifecycle
          isn't bound to popover open/close. */}
      <MutatorOnly>
        <input
          ref={uploadRef}
          type="file"
          hidden
          onChange={onFileChosen}
        />
      </MutatorOnly>
      {tab === 'preview' && (
        <button
          type="button"
          class="td-bar-icon-btn"
          onClick={() => (previewFullscreen.value = !previewFullscreen.value)}
          title={previewFullscreen.value ? 'Exit fullscreen (Esc)' : 'Fullscreen preview'}
          aria-label={previewFullscreen.value ? 'Exit fullscreen' : 'Fullscreen'}
        >
          <Icon name={previewFullscreen.value ? 'fullscreen-exit' : 'fullscreen'} size={14} />
        </button>
      )}
      <PopoverMenu
        align="right"
        trigger={({ onClick, ref, 'aria-expanded': ex }) => (
          <button
            type="button"
            class="td-bar-icon-btn"
            onClick={onClick}
            ref={ref as (el: HTMLButtonElement | null) => void}
            aria-expanded={ex}
            aria-haspopup="menu"
            title="Task settings + actions"
            aria-label="Open task menu"
          >
            <Icon name="settings" size={14} />
          </button>
        )}
      >
        {(close) => (
          <>
            <PopoverSection>Session</PopoverSection>
            <PopoverItem
              disabled={!isSessionTab || phase === 'preparing'}
              onClick={() => { triggerReattach(task.task_id); close(); }}
              hint="↻"
            >
              Reattach
            </PopoverItem>
            <MutatorOnly>
              <PopoverItem
                disabled={phase !== 'ready'}
                onClick={() => { pickFile(); close(); }}
              >
                Upload file…
              </PopoverItem>
            </MutatorOnly>
            <MutatorOnly>
              <PopoverItem
                disabled={phase !== 'ready'}
                onClick={() => { void toggleScrollMode(task.task_id); close(); }}
              >
                {scrollOn ? 'Exit scroll mode' : 'Scroll mode'}
              </PopoverItem>
            </MutatorOnly>
            <PopoverItem
              onClick={() => { void openTerminalInNewTab(task.task_id); close(); }}
            >
              Open in new tab
            </PopoverItem>
            <PopoverDivider />
            <PopoverSection>Task</PopoverSection>
            <PopoverItem
              disabled={busy}
              onClick={() => { onCopyLink(); close(); }}
            >
              {copied ? 'Copied' : 'Copy link'}
            </PopoverItem>
            <MutatorOnly>
              <PopoverItem disabled={busy} onClick={() => { onRename(); close(); }}>
                Rename
              </PopoverItem>
            </MutatorOnly>
            <MutatorOnly>
              <PopoverItem
                disabled={busy || !isLive}
                danger
                onClick={() => { onKill(); close(); }}
              >
                Kill session
              </PopoverItem>
            </MutatorOnly>
            {onClose && (
              <>
                <PopoverDivider />
                <PopoverItem onClick={() => { onClose(); close(); }}>
                  Close detail
                </PopoverItem>
              </>
            )}
          </>
        )}
      </PopoverMenu>
    </div>
  );
}
