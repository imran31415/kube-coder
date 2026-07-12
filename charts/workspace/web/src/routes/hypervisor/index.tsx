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
import { Chat } from './Chat';
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

  useEffect(() => {
    void initHypervisor();
    return () => closeThread();
  }, []);

  const cfg = config.value;
  const list = threads.value;
  const active = activeThreadId.value;
  const activeThread = list.find((t) => t.id === active) ?? null;
  const status = activeStatus.value;

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
    void openThread(id);
    setSidebarOpen(false);
  }

  return (
    <div class="route route-hypervisor" data-sidebar-open={sidebarOpen ? 'true' : 'false'}>
      <div class="hv-scrim" onClick={() => setSidebarOpen(false)} aria-hidden="true" />

      <aside class="hv-sidebar">
        <div class="hv-sidebar-head">
          <span class="hv-eyebrow">Chats</span>
          <Button
            size="sm"
            variant="primary"
            onClick={() => {
              newChat();
              setSidebarOpen(false);
            }}
            title="Start a new chat"
          >
            <Icon name="plus" size={12} /> New
          </Button>
        </div>

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
          {list.length === 0 && <p class="hv-thread-empty">No chats yet.</p>}
          {list.map((t) => (
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
                onClick={() => removeThread(t.id)}
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
              aria-label="Open chats"
            >
              <Icon name="more" size={18} />
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
