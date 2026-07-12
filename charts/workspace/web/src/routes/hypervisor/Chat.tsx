import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { Button } from '../../components/primitives/Button';
import { EmptyState } from '../../components/primitives/EmptyState';
import {
  events,
  activeThreadId,
  activeStatus,
  sending,
  stopping,
  chatError,
  selectedAssistant,
  config,
  sendMessage,
  stopMessage,
} from '../../store/hypervisor';
import { WorkspaceContext } from './WorkspaceContext';
import { buildTurns, renderMarkdown, type Block } from './transcript';

/**
 * The chat transcript + composer. The backend delivers a canonical event stream
 * (assistant prose, tool calls/results, errors); buildTurns() groups it into
 * user bubbles + agent turns, and we render prose as markdown and tool runs as
 * compact activity chips — so the conversation reads as the *Kube-Coder*
 * workspace, not a raw Claude/OpenCode terminal. No screen scraping.
 */

const SUGGESTIONS = [
  "What's running and how much CPU am I using?",
  'Spin up a task to run the tests',
  'Remember that I deploy with `make ship`',
];

/** One tool/command run — collapsed by default, expandable to the raw detail. */
function ActivityChip({ label, detail, error }: { label: string; detail: string; error?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div class={`hv-activity ${open ? 'is-open' : ''} ${error ? 'is-error' : ''}`}>
      <button type="button" class="hv-activity-head" onClick={() => setOpen((v) => !v)}>
        <span class="hv-activity-icon">
          <Icon name="terminal" size={12} />
        </span>
        <span class="hv-activity-label">{label}</span>
        <Icon name="chevron-down" size={13} class="hv-activity-caret" />
      </button>
      {open && detail && <pre class="hv-activity-detail">{detail}</pre>}
    </div>
  );
}

function AgentBlocks({ blocks }: { blocks: Block[] }) {
  return (
    <>
      {blocks.map((b, i) =>
        b.kind === 'prose' ? (
          <div
            key={i}
            class="hv-prose"
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{ __html: renderMarkdown(b.text) }}
          />
        ) : (
          <ActivityChip key={i} label={b.label} detail={b.detail} error={b.error} />
        ),
      )}
    </>
  );
}

export function Chat() {
  const [draft, setDraft] = useState('');
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const active = activeThreadId.value;
  const status = activeStatus.value;
  const evts = events.value;

  const turns = useMemo(() => buildTurns(evts), [evts]);
  const hasAgentTail = turns.length > 0 && turns[turns.length - 1].role === 'agent';

  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

  function submit(text?: string) {
    const value = (text ?? draft).trim();
    if (!value || blocked) return;
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
  // Input is locked whenever a turn is in flight — not just during the brief
  // send request — so the user can't queue a message the server would reject
  // (409 "assistant is still responding"). Stop is the only action then.
  const blocked = busy || working;
  const readOnly = config.value?.readOnly;
  const empty = !active && evts.length === 0;
  const cli = selectedAssistant.value || 'agent';
  // Show the thinking indicator while the agent is working, or right after we
  // sent and no assistant turn has landed yet.
  const thinking = working || (busy && active !== null && !hasAgentTail);

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
                  disabled={blocked}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div class="hv-transcript-flow">
            {turns.map((t, i) =>
              t.role === 'user' ? (
                <div key={i} class="hv-msg hv-msg-user">
                  <div class="hv-bubble">{t.text}</div>
                </div>
              ) : (
                <div key={i} class="hv-turn">
                  <div class="hv-avatar" aria-hidden="true">
                    <Icon name="hypervisor" size={15} />
                  </div>
                  <div class="hv-turn-body">
                    <div class="hv-turn-head">
                      <span class="hv-turn-name">Kube-Coder</span>
                      <span class="hv-turn-via">via {cli}</span>
                      {thinking && i === turns.length - 1 && (
                        <span class="hv-typing" aria-label="working">
                          <i />
                          <i />
                          <i />
                        </span>
                      )}
                    </div>
                    <AgentBlocks blocks={t.blocks} />
                  </div>
                </div>
              ),
            )}

            {/* Agent is working but hasn't emitted its turn block yet. */}
            {active && thinking && !hasAgentTail && (
              <div class="hv-turn">
                <div class="hv-avatar" aria-hidden="true">
                  <Icon name="hypervisor" size={15} />
                </div>
                <div class="hv-turn-body">
                  <div class="hv-turn-head">
                    <span class="hv-turn-name">Kube-Coder</span>
                    <span class="hv-turn-via">via {cli}</span>
                    <span class="hv-typing" aria-label="working">
                      <i />
                      <i />
                      <i />
                    </span>
                  </div>
                  <div class="hv-prose hv-prose-muted">Working…</div>
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
              : working
                ? 'Kube-Coder is working… press Stop to interrupt'
                : 'Message Kube-Coder…  (Enter to send, Shift+Enter for newline)'
          }
          onInput={(e) => setDraft((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKeyDown}
          rows={1}
          disabled={blocked}
        />
        {working ? (
          <Button
            type="button"
            variant="danger"
            onClick={() => void stopMessage()}
            disabled={stopping.value}
            title="Stop execution"
          >
            <Icon name="close" size={12} /> {stopping.value ? 'Stopping…' : 'Stop'}
          </Button>
        ) : (
          <Button type="submit" variant="primary" disabled={blocked || !draft.trim()} title="Send (Enter)">
            <Icon name="play" size={12} /> Send
          </Button>
        )}
      </form>
    </div>
  );
}
