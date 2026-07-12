import { useEffect } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { Button } from '../../components/primitives/Button';
import {
  config,
  configError,
  threads,
  activeThreadId,
  selectedAssistant,
  initHypervisor,
  openThread,
  newChat,
  removeThread,
  closeThread,
} from '../../store/hypervisor';
import { Chat } from './Chat';
import './hypervisor.css';

export function HypervisorRoute() {
  useEffect(() => {
    void initHypervisor();
    return () => closeThread();
  }, []);

  const cfg = config.value;
  const list = threads.value;
  const active = activeThreadId.value;

  if (cfg && cfg.enabled === false) {
    return (
      <div class="route route-hypervisor">
        <div class="hv-empty">
          <Icon name="hypervisor" size={32} />
          <h2>Hypervisor is disabled</h2>
          <p class="muted">
            Enable it in the workspace chart (<code>hypervisor.enabled</code>).
          </p>
        </div>
      </div>
    );
  }

  return (
    <div class="route route-hypervisor">
      <aside class="hv-sidebar">
        <div class="hv-sidebar-head">
          <span class="hv-sidebar-title">Chats</span>
          <Button
            size="sm"
            variant="primary"
            onClick={() => newChat()}
            title="Start a new chat"
          >
            <Icon name="plus" size={12} /> New
          </Button>
        </div>

        {/* Which CLI agent a new chat uses. Any enabled assistant works — the
            chat is a clean layer over the agent the user already configures. */}
        <label class="hv-agent-picker">
          <span class="muted">Agent</span>
          <select
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
          {list.length === 0 && (
            <p class="muted hv-thread-empty">No chats yet.</p>
          )}
          {list.map((t) => (
            <div
              key={t.id}
              class={`hv-thread ${active === t.id ? 'hv-thread-active' : ''}`}
            >
              <button
                type="button"
                class="hv-thread-open"
                onClick={() => openThread(t.id)}
                title={t.title}
              >
                <span class={`hv-status-dot hv-status-${t.status}`} aria-hidden="true" />
                <span class="hv-thread-title">{t.title || 'New chat'}</span>
                <span class="hv-thread-agent muted">{t.assistant}</span>
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
        {configError.value && (
          <div class="hv-banner hv-banner-error">{configError.value}</div>
        )}
        <Chat />
      </section>
    </div>
  );
}
