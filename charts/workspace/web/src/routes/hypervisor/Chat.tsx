import { useEffect, useRef, useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { Button } from '../../components/primitives/Button';
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
 * The chat transcript + composer. User turns render as bubbles; the agent's
 * live rendered output is shown as an assistant response block (polled). This
 * is intentionally CLI-agnostic — any selected agent streams here. Structured
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
    // Enter sends; Shift+Enter (or the mobile keyboard's newline) inserts a
    // line break. Cmd/Ctrl+Enter also sends for muscle-memory parity.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  const busy = sending.value;
  const working = status === 'running';
  const readOnly = config.value?.readOnly;
  const empty = !active && msgs.length === 0;

  return (
    <div class="hv-chat">
      <div class="hv-transcript" ref={scrollRef}>
        {empty && (
          <div class="hv-welcome">
            <Icon name="hypervisor" size={30} />
            <h2>Workspace Hypervisor</h2>
            <p class="muted">
              Ask about your workspace or tell it what to do — "how many tasks are
              running and what's my CPU?", "spin up a task to run the tests",
              "remember that I deploy with <code>make ship</code>".
            </p>
            <p class="muted hv-welcome-agent">
              Powered by <strong>{selectedAssistant.value || 'your agent'}</strong>.
            </p>
          </div>
        )}

        {msgs.map((m, i) => (
          <div key={i} class={`hv-msg hv-msg-${m.role}`}>
            <div class="hv-msg-body">{m.text}</div>
          </div>
        ))}

        {/* The agent's live rendered output — its answer + any tool activity. */}
        {active && output && (
          <div class="hv-msg hv-msg-assistant hv-msg-output">
            <div class="hv-msg-role">
              <Icon name="hypervisor" size={12} />
              <span>{status ? status : 'assistant'}</span>
              {working && <span class="hv-typing" aria-hidden="true" />}
            </div>
            <pre class="hv-output">{output}</pre>
          </div>
        )}
      </div>

      {chatError.value && (
        <div class="hv-banner hv-banner-error">{chatError.value}</div>
      )}

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
          placeholder={readOnly
            ? 'Read-only workspace — you can still ask about state'
            : 'Message the Hypervisor…  (Enter to send, Shift+Enter for newline)'}
          onInput={(e) => setDraft((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKeyDown}
          rows={1}
          disabled={busy}
        />
        <Button
          type="submit"
          variant="primary"
          disabled={busy || !draft.trim()}
          title="Send (Enter)"
        >
          <Icon name="play" size={12} /> Send
        </Button>
      </form>
    </div>
  );
}
