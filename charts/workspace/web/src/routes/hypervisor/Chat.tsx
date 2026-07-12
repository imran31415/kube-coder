import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
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
import { WorkspaceContext } from './WorkspaceContext';
import { parseAgentTranscript, renderMarkdown, type TranscriptBlock } from './transcript';

/**
 * The chat transcript + composer. User turns render as bubbles; the CLI agent's
 * polled pane is parsed (see transcript.ts) into clean, system-branded blocks —
 * assistant prose as markdown bubbles, tool/command runs as compact activity
 * chips — so the conversation reads as coming from the *Kube-Coder* workspace,
 * not a raw Claude/OpenCode terminal.
 */

const SUGGESTIONS = [
  "What's running and how much CPU am I using?",
  'Spin up a task to run the tests',
  'Remember that I deploy with `make ship`',
];

/** One tool/command run — collapsed by default, expandable to the raw detail. */
function ActivityChip({ label, detail }: { label: string; detail: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div class={`hv-activity ${open ? 'is-open' : ''}`}>
      <button type="button" class="hv-activity-head" onClick={() => setOpen((v) => !v)}>
        <span class="hv-activity-icon">
          <Icon name="terminal" size={12} />
        </span>
        <span class="hv-activity-label">{label}</span>
        <Icon name="chevron-down" size={13} class="hv-activity-caret" />
      </button>
      {open && <pre class="hv-activity-detail">{detail}</pre>}
    </div>
  );
}

function AgentBlocks({ blocks }: { blocks: TranscriptBlock[] }) {
  return (
    <>
      {blocks.map((b, i) =>
        b.kind === 'text' ? (
          <div
            key={i}
            class="hv-prose"
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{ __html: renderMarkdown(b.text) }}
          />
        ) : (
          <ActivityChip key={i} label={b.label} detail={b.detail} />
        ),
      )}
    </>
  );
}

export function Chat() {
  const [draft, setDraft] = useState('');
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const msgs = messages.value;
  const output = liveOutput.value;
  const active = activeThreadId.value;
  const status = activeStatus.value;

  const blocks = useMemo(() => (active ? parseAgentTranscript(output) : []), [active, output]);

  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs, blocks]);

  function submit(text?: string) {
    const value = (text ?? draft).trim();
    if (!value || sending.value) return;
    setDraft('');
    void sendMessage(value);
    taRef.current?.focus();
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  const busy = sending.value;
  const working = status === 'running';
  const readOnly = config.value?.readOnly;
  const empty = !active && msgs.length === 0;
  const cli = selectedAssistant.value || 'agent';
  // A turn is "in flight" (show the thinking indicator) while the agent is
  // working, or right after we sent and no output has landed yet.
  const thinking = working || (busy && active !== null && blocks.length === 0);

  return (
    <div class="hv-chat">
      {active && <WorkspaceContext />}

      <div class="hv-transcript" ref={scrollRef}>
        {empty ? (
          <div class="hv-welcome-host">
            <EmptyState
              icon={<Icon name="hypervisor" size={26} />}
              title="Kube-Coder"
              description={
                <>
                  Ask about your workspace or tell it what to do — it reads live
                  state and acts on it through your tools.
                </>
              }
            />
            <div class="hv-suggests">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  class="hv-suggest"
                  onClick={() => submit(s)}
                  disabled={busy}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div class="hv-transcript-flow">
            {msgs.map((m, i) =>
              m.role === 'user' ? (
                <div key={i} class="hv-msg hv-msg-user">
                  <div class="hv-bubble">{m.text}</div>
                </div>
              ) : (
                <div key={i} class="hv-msg hv-msg-agent">
                  <div class="hv-bubble hv-bubble-agent">{m.text}</div>
                </div>
              ),
            )}

            {/* The live agent turn — parsed into clean system-branded blocks. */}
            {active && (blocks.length > 0 || thinking) && (
              <div class="hv-turn">
                <div class="hv-avatar" aria-hidden="true">
                  <Icon name="hypervisor" size={15} />
                </div>
                <div class="hv-turn-body">
                  <div class="hv-turn-head">
                    <span class="hv-turn-name">Kube-Coder</span>
                    <span class="hv-turn-via">via {cli}</span>
                    {thinking && (
                      <span class="hv-typing" aria-label="working">
                        <i />
                        <i />
                        <i />
                      </span>
                    )}
                  </div>
                  {blocks.length > 0 ? (
                    <AgentBlocks blocks={blocks} />
                  ) : (
                    <div class="hv-prose hv-prose-muted">Working…</div>
                  )}
                </div>
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
              : 'Message Kube-Coder…  (Enter to send, Shift+Enter for newline)'
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
