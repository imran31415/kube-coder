import { useEffect, useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { EmptyState } from '../../components/primitives/EmptyState';
import { useIsMobile } from '../../hooks/useMediaQuery';
import {
  config,
  configError,
  threads,
  activeThreadId,
  activeStatus,
  selectedAssistant,
  initHypervisor,
  openThread,
  newChat,
  removeThread,
  closeThread,
} from '../../store/hypervisor';
import type { ThreadStatus } from '../../api/hypervisor';
import { currentPath, navigate, pathSuffix } from '../../store/router';
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

  useEffect(() => {
    void initHypervisor();
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
            onChange={(e) => (selectedAssistant.value = (e.target as HTMLSelectElement).value)}
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

        <div class="hv-thread-list">
          {shown.length === 0 && (
            <p class="hv-thread-empty">
              {chatTab === 'active' ? 'No active chats — start one with New.' : 'No past chats.'}
            </p>
          )}
          {shown.map((t) => (
            <div key={t.id} class={`hv-thread ${active === t.id ? 'hv-thread-active' : ''}`}>
              <button
                type="button"
                class="hv-thread-open"
                onClick={() => pick(t.id)}
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
                class="hv-thread-del"
                title="Delete chat"
                aria-label="Delete chat"
                onClick={() => {
                  // If we're deleting the open thread, drop back to the
                  // new-chat URL so the route doesn't try to re-open a
                  // now-missing id.
                  if (active === t.id) navigate('/hypervisor');
                  void removeThread(t.id);
                }}
              >
                <Icon name="close" size={12} />
              </button>
            </div>
          ))}
        </div>
      </aside>

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
          <div class="hv-topbar-meta">
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

        <Chat />
      </section>
    </div>
  );
}
