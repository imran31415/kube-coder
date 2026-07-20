import { useEffect, useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { GuidePanel } from '../../components/GuidePanel';
import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { EmptyState } from '../../components/primitives/EmptyState';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { useIsMobile } from '../../hooks/useMediaQuery';
import {
  config,
  configError,
  threads,
  deletedThreads,
  activeThreadId,
  activeStatus,
  selectedAssistant,
  selectedModel,
  selectedWorkdir,
  assistantModels,
  setSelectedAssistant,
  setActiveThreadModel,
  initHypervisor,
  openThread,
  newChat,
  removeThread,
  reviveThread,
  refreshDeletedThreads,
  renameThreadTitle,
  closeThread,
} from '../../store/hypervisor';
import type { ThreadStatus, HypervisorThread } from '../../api/hypervisor';
import { listWorkdirs, type WorkdirOption } from '../../api/tasks';
import { currentPath, navigate, pathSuffix, routeHref } from '../../store/router';
import { Chat } from './Chat';
import { partitionThreads, type ChatTab } from './chatTabs';
import './hypervisor.css';

const STATUS_TONE: Record<string, 'neutral' | 'success' | 'warn' | 'danger'> = {
  running: 'success',
  error: 'danger',
  idle: 'neutral',
};

function statusLabel(s: string): string {
  if (s === 'running') return 'thinking';
  return s || 'idle';
}

export function HypervisorRoute() {
  const isMobile = useIsMobile();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [chatTab, setChatTab] = useState<ChatTab>('active');
  // The chat awaiting delete-confirmation (null when the dialog is closed).
  const [pendingDelete, setPendingDelete] = useState<HypervisorThread | null>(null);
  // "Recently deleted" is collapsed by default; expanding it lazy-loads the
  // tombstones so the common case never pays for the extra request.
  const [trashOpen, setTrashOpen] = useState(false);
  // Inline rename: the thread whose title is being edited, plus its draft text.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  // Workspace folders for the new-chat workdir picker (#345). Same source as
  // the Build tab's picker; empty list → free-text fallback.
  const [dirs, setDirs] = useState<WorkdirOption[]>([]);

  useEffect(() => {
    void initHypervisor();
    listWorkdirs().then(setDirs).catch(() => setDirs([]));
    return () => closeThread();
  }, []);

  // URL-driven thread selection: `/hypervisor/<id>` opens that thread, a bare
  // `/hypervisor` shows the new-chat state. This makes the Desktop composer
  // (and Activity widget) able to deep-link straight into a chat, and keeps
  // back/forward + refresh honest. Mirrors how the Tasks route reads its id.
  useEffect(() => {
    const id = pathSuffix(currentPath.value).split('/')[0] || null;
    if (id) {
      if (id !== activeThreadId.value) void openThread(id);
    } else if (activeThreadId.value) {
      newChat();
    }
  }, [currentPath.value]);

  const cfg = config.value;
  const list = threads.value;
  const active = activeThreadId.value;
  const activeThread = list.find((t) => t.id === active) ?? null;
  const status = activeStatus.value;

  // Model switcher (#308): an open thread uses its own assistant + stored model;
  // a not-yet-created chat uses the sidebar's assistant + new-thread default.
  const effectiveAssistant = activeThread?.assistant || selectedAssistant.value;
  const models = assistantModels(effectiveAssistant);
  const currentModel = active
    ? activeThread?.model || models[0] || ''
    : selectedModel.value || models[0] || '';

  // Split into what you're working with now vs. older chats. Derived purely
  // from status + updated_at (see chatTabs.ts) — no server change needed.
  const { active: activeThreads, past: pastThreads } = partitionThreads(
    list,
    active,
    Date.now(),
  );
  const shown = chatTab === 'active' ? activeThreads : pastThreads;

  // If there's nothing to show under Active but there is history, land the user
  // on Past so the list isn't misleadingly empty. Only nudges while sitting on
  // an empty Active tab, so a deliberate switch back isn't fought.
  useEffect(() => {
    if (chatTab === 'active' && activeThreads.length === 0 && pastThreads.length > 0) {
      setChatTab('past');
    }
  }, [chatTab, activeThreads.length, pastThreads.length]);

  // Lazy-load the trash the first time the user opens "Recently deleted".
  useEffect(() => {
    if (trashOpen) void refreshDeletedThreads();
  }, [trashOpen]);

  if (cfg && cfg.enabled === false) {
    return (
      <div class="route route-hypervisor">
        <EmptyState
          icon={<Icon name="hypervisor" size={26} />}
          title="Hypervisor is disabled"
          description={
            <>
              Enable it in the workspace chart (<code>hypervisor.enabled</code>).
            </>
          }
        />
      </div>
    );
  }

  function pick(id: string) {
    // Drive selection through the URL so refresh/back/forward stay honest;
    // the path effect above calls openThread(id).
    navigate(`/hypervisor/${encodeURIComponent(id)}`);
    setSidebarOpen(false);
  }

  function startRename(id: string, title: string) {
    setRenamingId(id);
    setDraftTitle(title || '');
  }

  function cancelRename() {
    setRenamingId(null);
    setDraftTitle('');
  }

  function commitRename(id: string) {
    const next = draftTitle.trim();
    if (next) void renameThreadTitle(id, next);
    cancelRename();
  }

  return (
    <div class="route route-hypervisor" data-sidebar-open={sidebarOpen ? 'true' : 'false'}>
      <div class="hv-scrim" onClick={() => setSidebarOpen(false)} aria-hidden="true" />

      <aside class="hv-sidebar">
        <div class="hv-sidebar-head">
          <span class="hv-eyebrow">Chats</span>
          <div class="hv-sidebar-head-actions">
            <Button
              size="sm"
              variant="primary"
              onClick={() => {
                navigate('/hypervisor');
                setSidebarOpen(false);
              }}
              title="Start a new chat"
            >
              <Icon name="plus" size={12} /> New
            </Button>
            <button
              type="button"
              class="hv-sidebar-close"
              onClick={() => setSidebarOpen(false)}
              title="Close"
              aria-label="Close chats"
            >
              <Icon name="close" size={16} />
            </button>
          </div>
        </div>

        {/* Default to just the chats in play; older ones live under Past so the
            list stays short. Split is derived (chatTabs.ts), not a stored flag. */}
        <nav class="hv-tabs" role="tablist" aria-label="Chats filter">
          <button
            type="button"
            role="tab"
            aria-selected={chatTab === 'active'}
            class={`hv-tab ${chatTab === 'active' ? 'hv-tab-active' : ''}`}
            onClick={() => setChatTab('active')}
          >
            Active
            {activeThreads.length > 0 && <span class="hv-tab-count">{activeThreads.length}</span>}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={chatTab === 'past'}
            class={`hv-tab ${chatTab === 'past' ? 'hv-tab-active' : ''}`}
            onClick={() => setChatTab('past')}
          >
            Past
            {pastThreads.length > 0 && <span class="hv-tab-count">{pastThreads.length}</span>}
          </button>
        </nav>

        {/* Which CLI agent a new chat uses — any enabled assistant. The chat is
            a clean layer over the agent the user already configures. */}
        <label class="hv-agent-picker">
          <span class="hv-eyebrow">Agent</span>
          <select
            class="hv-agent-select"
            value={selectedAssistant.value}
            onChange={(e) => setSelectedAssistant((e.target as HTMLSelectElement).value)}
            aria-label="Chat agent"
          >
            {(cfg?.assistants ?? []).map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
                {a.model ? ` · ${a.model}` : ''}
              </option>
            ))}
          </select>
        </label>

        {/* Where a NEW chat starts (#345). The backend has always accepted a
            per-thread workdir; this picker finally passes it, so starting an
            agent in a repo no longer burns a first message on `cd`. An open
            thread keeps the folder it was created in. */}
        <label class="hv-agent-picker">
          <span class="hv-eyebrow">Folder</span>
          {dirs.length > 0 ? (
            <select
              class="hv-agent-select"
              value={selectedWorkdir.value}
              onChange={(e) => (selectedWorkdir.value = (e.target as HTMLSelectElement).value)}
              aria-label="Folder for new chats"
              title="The folder a new chat starts in"
            >
              {/* Keep the current value selectable even when it isn't in the
                  server's list (e.g. a custom HYPERVISOR_WORKDIR). */}
              {selectedWorkdir.value && !dirs.some((d) => d.path === selectedWorkdir.value) && (
                <option value={selectedWorkdir.value}>{selectedWorkdir.value}</option>
              )}
              {dirs.map((d) => (
                <option key={d.path} value={d.path}>
                  {d.label ?? d.path}
                  {d.is_git ? '  (git)' : ''}
                </option>
              ))}
            </select>
          ) : (
            <input
              class="hv-agent-select"
              value={selectedWorkdir.value}
              onInput={(e) => (selectedWorkdir.value = (e.target as HTMLInputElement).value)}
              aria-label="Folder for new chats"
              placeholder={cfg?.workdir || '/home/dev'}
            />
          )}
        </label>

        <div class="hv-thread-list">
          {shown.length === 0 && (
            <p class="hv-thread-empty">
              {chatTab === 'active' ? 'No active chats — start one with New.' : 'No past chats.'}
            </p>
          )}
          {shown.map((t) =>
            renamingId === t.id ? (
              <div key={t.id} class={`hv-thread hv-thread-renaming ${active === t.id ? 'hv-thread-active' : ''}`}>
                <input
                  class="hv-thread-rename-input"
                  value={draftTitle}
                  autoFocus
                  maxLength={80}
                  aria-label="Chat name"
                  onInput={(e) => setDraftTitle((e.target as HTMLInputElement).value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') commitRename(t.id);
                    else if (e.key === 'Escape') cancelRename();
                  }}
                  onBlur={() => commitRename(t.id)}
                />
                <button
                  type="button"
                  class="hv-thread-rename-save"
                  title="Save name"
                  aria-label="Save name"
                  // mousedown fires before the input's blur, so the click isn't
                  // swallowed by the blur-commit teardown.
                  onMouseDown={(e) => {
                    e.preventDefault();
                    commitRename(t.id);
                  }}
                >
                  <Icon name="check" size={12} />
                </button>
              </div>
            ) : (
              <div key={t.id} class={`hv-thread ${active === t.id ? 'hv-thread-active' : ''}`}>
                <button
                  type="button"
                  class="hv-thread-open"
                  onClick={() => pick(t.id)}
                  onDblClick={() => startRename(t.id, t.title)}
                  title={t.title}
                >
                  <span class={`hv-dot hv-dot-${t.status}`} aria-hidden="true" />
                  <span class="hv-thread-body">
                    <span class="hv-thread-title">{t.title || 'New chat'}</span>
                    <span class="hv-thread-agent">{t.assistant}</span>
                  </span>
                </button>
                <button
                  type="button"
                  class="hv-thread-rename"
                  title="Rename chat"
                  aria-label="Rename chat"
                  onClick={() => startRename(t.id, t.title)}
                >
                  <Icon name="pencil" size={12} />
                </button>
                <button
                  type="button"
                  class="hv-thread-del"
                  title="Delete chat"
                  aria-label="Delete chat"
                  onClick={() => setPendingDelete(t)}
                >
                  <Icon name="close" size={12} />
                </button>
              </div>
            ),
          )}
        </div>

        {/* Recently deleted — a collapsible trash so an accidental delete is
            recoverable (issue #260). Soft-deleted threads keep their files;
            Restore clears the tombstone. GC hard-purges old ones server-side. */}
        <div class={`hv-trash ${trashOpen ? 'hv-trash-open' : ''}`}>
          <button
            type="button"
            class="hv-trash-toggle"
            aria-expanded={trashOpen}
            onClick={() => setTrashOpen((v) => !v)}
          >
            <Icon name={trashOpen ? 'chevron-down' : 'chevron-right'} size={12} />
            <span>Recently deleted</span>
            {deletedThreads.value.length > 0 && (
              <span class="hv-tab-count">{deletedThreads.value.length}</span>
            )}
          </button>
          {trashOpen && (
            <div class="hv-trash-list">
              {deletedThreads.value.length === 0 && (
                <p class="hv-thread-empty">Nothing here.</p>
              )}
              {deletedThreads.value.map((t) => (
                <div key={t.id} class="hv-thread hv-thread-deleted">
                  <span class="hv-thread-open hv-thread-open-static" title={t.title}>
                    <span class="hv-dot hv-dot-deleted" aria-hidden="true" />
                    <span class="hv-thread-body">
                      <span class="hv-thread-title">{t.title || 'New chat'}</span>
                      <span class="hv-thread-agent">{t.assistant}</span>
                    </span>
                  </span>
                  <button
                    type="button"
                    class="hv-thread-restore"
                    title="Restore chat"
                    onClick={() => void reviveThread(t.id)}
                  >
                    Restore
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </aside>

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this chat?"
        body="You can restore it from Recently deleted."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          const t = pendingDelete;
          setPendingDelete(null);
          if (!t) return;
          // If we're deleting the open thread, drop back to the new-chat URL so
          // the route doesn't try to re-open a now-missing id.
          if (active === t.id) navigate('/hypervisor');
          void removeThread(t.id);
        }}
        onCancel={() => setPendingDelete(null)}
      />

      <section class="hv-main">
        <header class="hv-topbar">
          {isMobile && (
            <button
              type="button"
              class="hv-topbar-menu"
              onClick={() => setSidebarOpen((v) => !v)}
              title="Chats"
              aria-label={`Chats (${list.length})`}
            >
              <Icon name="chat" size={15} />
              <span class="hv-topbar-menu-label">Chats</span>
              {list.length > 0 && <span class="hv-topbar-menu-count">{list.length}</span>}
            </button>
          )}
          <span class="hv-topbar-title">
            {activeThread ? activeThread.title || 'Chat' : 'Kube-Coder'}
          </span>
          {/* Walkie-Talkie is now its own top-level tab (/walkie); keep a link
              here so the loopback preview is still one hop from a chat. */}
          <a
            class="hv-walkie-link"
            href={routeHref('/walkie')}
            onClick={(e) => {
              e.preventDefault();
              navigate('/walkie');
            }}
            title="Open the Walkie-Talkie — talk to your workspace over the internal loopback"
          >
            <Icon name="walkie" size={13} /> Walkie-Talkie
          </a>
          <div class="hv-topbar-meta">
            {/* Model switcher (#308) — works for a not-yet-created chat (sets the
                new-thread default) and for the open thread (switches it live; the
                change lands on the next turn). Shown only when the effective
                assistant offers a model choice. */}
            {models.length > 0 && (
              <label class="hv-model-picker" title={currentModel ? `Model: ${currentModel}` : 'Model for this chat'}>
                <span class="hv-model-label">Model</span>
                <select
                  class="hv-model-select"
                  value={currentModel}
                  disabled={status === 'running'}
                  onChange={(e) =>
                    void setActiveThreadModel((e.target as HTMLSelectElement).value)
                  }
                  aria-label="Chat model"
                >
                  {models.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {active && status && (
              <Pill tone={STATUS_TONE[status] ?? 'neutral'}>
                {statusLabel(status as ThreadStatus)}
              </Pill>
            )}
            {(activeThread?.assistant || selectedAssistant.value) && (
              <Pill mono>{activeThread?.assistant || selectedAssistant.value}</Pill>
            )}
          </div>
        </header>

        {configError.value && <div class="hv-banner hv-banner-error">{configError.value}</div>}

        <GuidePanel
          title="How the Hypervisor works"
          storageKey="kc.guide.hypervisor"
          intro="The Hypervisor is a chat layer over your workspace's coding agents. Every chat is a real agent session (Claude, OpenCode, …) that reads your live workspace and acts on it through the same tools you use — not a sandboxed toy."
          steps={[
            {
              title: 'Pick an agent, then New',
              body: 'Choose the CLI agent (plus model and starting folder) in the left sidebar, then start a chat with New.',
            },
            {
              title: 'Ask or instruct',
              body: 'Ask about your workspace or hand it a task — it reads live state (files, git, running apps) and acts on it.',
            },
            {
              title: 'Watch it work',
              body: 'The status pill shows thinking → idle. Switch the model mid-thread; the change lands on the next turn.',
            },
            {
              title: 'Everything persists',
              body: 'Chats are saved under Active and Past. Rename, delete, or restore them from Recently deleted anytime.',
            },
          ]}
          scenarios={[
            { prompt: 'What is running on this workspace right now?', outcome: 'lists the live loopback ports and apps' },
            { prompt: 'Clone imran31415/pool-hall and run its tests', outcome: 'clones, installs, runs, and reports back' },
            { prompt: 'Open a PR for the change on my branch', outcome: 'pushes and opens a GitHub PR with gh' },
          ]}
        />

        <Chat />
      </section>
    </div>
  );
}
