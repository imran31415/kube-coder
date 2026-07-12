import { useEffect, useRef, useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { Button } from '../../components/primitives/Button';
import { EmptyState } from '../../components/primitives/EmptyState';
import {
  messages,
  liveOutput,
  activeThreadId,
  activeStatus,
  sending,
  chatError,
  selectedAssistant,
  config,
  sendMessage,
} from '../../store/hypervisor';

/**
 * The chat transcript + composer. User turns render as neutral bubbles; the
 * agent's live rendered output is shown as a "live feed" card (polled). This is
 * intentionally CLI-agnostic — any selected agent streams here. Structured
 * per-CLI bubble rendering (stream-json etc.) is a follow-up.
 */
export function Chat() {
  const [draft, setDraft] = useState('');
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const msgs = messages.value;
  const output = liveOutput.value;
  const active = activeThreadId.value;
  const status = activeStatus.value;

  // Auto-grow the composer to fit its content (up to the CSS max-height).
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft]);

  // Auto-scroll to the newest content as the conversation / output grows.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs, output]);

  function submit() {
    const text = draft.trim();
    if (!text || sending.value) return;
    setDraft('');
    void sendMessage(text);
    taRef.current?.focus();
  }

  function onKeyDown(e: KeyboardEvent) {
    // Enter sends; Shift+Enter inserts a newline (standard chat behaviour).
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  const busy = sending.value;
  const working = status === 'running';
  const readOnly = config.value?.readOnly;
  const empty = !active && msgs.length === 0;
  const agentName = selectedAssistant.value || 'agent';

  return (
    <div class="hv-chat">
      <div class="hv-transcript" ref={scrollRef}>
        {empty ? (
          <div class="hv-welcome-host">
            <EmptyState
              icon={<Icon name="hypervisor" size={26} />}
              title="Workspace Hypervisor"
              description={
                <>
                  Ask about your workspace or tell it what to do — "how many tasks
                  are running and what's my CPU?", "spin up a task to run the
                  tests", "remember that I deploy with <code>make ship</code>".
                  <span class="hv-welcome-agent">
                    Powered by <strong>{agentName}</strong>.
                  </span>
                </>
              }
            />
          </div>
        ) : (
          <div class="hv-transcript-flow">
            {msgs.map((m, i) => (
              <div key={i} class={`hv-msg hv-msg-${m.role}`}>
                <div class="hv-bubble">{m.text}</div>
              </div>
            ))}

            {/* The agent's live rendered output — its answer + tool activity. */}
            {active && output && (
              <div class="hv-agent-card">
                <div class="hv-agent-head">
                  <Icon name="hypervisor" size={12} />
                  <span class="hv-agent-name">{agentName}</span>
                  {status && status !== 'running' && (
                    <span class="hv-agent-status">· {status}</span>
                  )}
                  {working && (
                    <span class="hv-typing" aria-label="working">
                      <i />
                      <i />
                      <i />
                    </span>
                  )}
                </div>
                <pre class="hv-output">{output}</pre>
              </div>
            )}
          </div>
        )}
      </div>

      {chatError.value && <div class="hv-banner hv-banner-error">{chatError.value}</div>}

      <form
        class="hv-composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          ref={taRef}
          class="hv-composer-input"
          value={draft}
          placeholder={
            readOnly
              ? 'Read-only workspace — you can still ask about state'
              : 'Message the Hypervisor…  (Enter to send, Shift+Enter for newline)'
          }
          onInput={(e) => setDraft((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKeyDown}
          rows={1}
          disabled={busy}
        />
        <Button type="submit" variant="primary" disabled={busy || !draft.trim()} title="Send (Enter)">
          <Icon name="play" size={12} /> Send
        </Button>
      </form>
    </div>
  );
}
