import { useState } from 'preact/hooks';
import type { MissionCard as Card, MissionPromptOption } from '../../api/mission';
import { stopThread } from '../../api/hypervisor';
import { killTask, sendFollowup } from '../../store/tasks';
import { refreshMission } from '../../store/mission';
import { navigate } from '../../store/router';
import { pushToast } from '../../store/ui';
import { MutatorOnly } from '../../components/MutatorOnly';
import { ConfirmDialog } from '../../components/ConfirmDialog';

const KIND_LABEL: Record<Card['kind'], string> = {
  build: 'BUILD',
  chat: 'CHAT',
  subagent: 'SUB-AGENT',
};

/** Short duration label like "42s" / "14m" / "2h" / "3d". */
function rel(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/** State-appropriate age: elapsed for running, wait time for waiting,
 *  "ago" for finished cards. '' when the source timestamp is missing. */
function timeLabel(card: Card): string {
  const now = Date.now() / 1000;
  if (card.state === 'running') {
    // U+FE0E forces text presentation so the glyphs never render as emoji.
    return card.created_at ? `▶︎ ${rel(now - card.created_at)}` : '';
  }
  if (card.state === 'waiting') {
    const since = card.waiting_since ?? card.updated_at;
    return since ? `${rel(now - since)} waiting` : '';
  }
  const at = card.finished_at ?? card.updated_at;
  return at ? `${rel(now - at)} ago` : '';
}

/** Route for a card (or a lineage reference by namespaced id). */
function cardHref(id: string): string {
  const [kind, ref] = id.split(/:(.*)/s);
  return kind === 'chat' ? `/hypervisor/${ref}` : `/tasks/${ref}`;
}

/**
 * One board card. The whole body is clickable (same as Open) — footer buttons
 * and quick-replies stop propagation so actions never double as navigation.
 */
export function MissionCard({ card }: { card: Card }) {
  const [confirmKill, setConfirmKill] = useState(false);
  const [busy, setBusy] = useState(false);

  const live = card.state === 'running' || card.state === 'waiting';
  const isTask = card.kind !== 'chat';
  const open = () => navigate(cardHref(card.id));

  async function onKill() {
    setConfirmKill(false);
    setBusy(true);
    try {
      // killTask (store) already toasts + refreshes the Build list.
      await killTask(card.ref_id);
      await refreshMission();
    } finally {
      setBusy(false);
    }
  }

  async function onStopChat() {
    setBusy(true);
    try {
      await stopThread(card.ref_id);
      pushToast('Stop requested', { kind: 'warn' });
      await refreshMission();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Stop failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  // Same wire format as MessageChat's quick replies: send the option's key
  // ("2", "y", …) as a follow-up so tmux forwards the keystroke to the TUI.
  async function onChoose(opt: MissionPromptOption) {
    setBusy(true);
    try {
      await sendFollowup(card.ref_id, String(opt.index));
      await refreshMission();
    } finally {
      setBusy(false);
    }
  }

  return (
    <article
      class={`mission-card mission-card-${card.state} ${card.state === 'waiting' ? 'mission-card-attn' : ''}`}
      onClick={open}
      data-card-id={card.id}
    >
      <div class="mission-card-row">
        <span class="mission-kind">{KIND_LABEL[card.kind]}</span>
        {(card.assistant || card.model) && (
          <span class="mission-agent">
            {card.assistant}
            {card.assistant && card.model ? ' · ' : ''}
            {card.model}
          </span>
        )}
        <span class={`mission-time ${card.state === 'waiting' ? 'mission-time-warn' : ''}`}>
          {timeLabel(card)}
        </span>
      </div>

      {card.outcome && (
        <div class={`mission-outcome ${card.outcome.ok ? 'mission-outcome-ok' : 'mission-outcome-bad'}`}>
          {card.outcome.ok ? '✓' : '✕'} {card.outcome.detail}
        </div>
      )}

      <div class="mission-card-title">{card.title}</div>
      {card.headline && <div class="mission-card-headline muted">{card.headline}</div>}

      {(card.repo || card.branch) && (
        <div class="mission-card-meta">
          {card.repo && <span>{card.repo}</span>}
          {card.branch && <span class="mission-branch">⎇ {card.branch}</span>}
        </div>
      )}

      {card.children.length > 0 && (
        <div class="mission-lineage">
          └ {card.children.length} sub-agent{card.children.length === 1 ? '' : 's'}:{' '}
          {card.children.map((child, i) => (
            <span key={child.id}>
              {i > 0 && ' · '}
              <button
                type="button"
                class="mission-lineage-link"
                onClick={(e) => {
                  e.stopPropagation();
                  navigate(cardHref(child.id));
                }}
              >
                {child.title}
              </button>
            </span>
          ))}
        </div>
      )}
      {card.parent_id && (
        <div class="mission-lineage">
          ↳ spawned by{' '}
          <button
            type="button"
            class="mission-lineage-link"
            onClick={(e) => {
              e.stopPropagation();
              navigate(cardHref(card.parent_id as string));
            }}
          >
            {card.parent_id.split(/:(.*)/s)[1]}
          </button>
        </div>
      )}

      {card.state === 'waiting' && card.waiting_prompt && (
        <div class="mission-prompt" onClick={(e) => e.stopPropagation()}>
          <div class="mission-prompt-label">needs your input</div>
          {card.waiting_prompt.question && (
            <div class="mission-prompt-q">{card.waiting_prompt.question}</div>
          )}
          <MutatorOnly>
            <div class="mission-prompt-replies">
              {card.waiting_prompt.options.map((o) => (
                <button
                  key={String(o.index)}
                  type="button"
                  class="mission-reply"
                  disabled={busy}
                  onClick={() => void onChoose(o)}
                >
                  {o.label}
                </button>
              ))}
            </div>
          </MutatorOnly>
        </div>
      )}

      <div class="mission-card-foot" onClick={(e) => e.stopPropagation()}>
        <button type="button" class="mission-foot-btn mission-foot-primary" onClick={open}>
          Open
        </button>
        <div class="mission-foot-right">
          {live && isTask && (
            <MutatorOnly>
              <button
                type="button"
                class="mission-foot-btn mission-foot-danger"
                disabled={busy}
                onClick={() => setConfirmKill(true)}
              >
                Kill
              </button>
            </MutatorOnly>
          )}
          {card.kind === 'chat' && card.state === 'running' && (
            <MutatorOnly>
              <button
                type="button"
                class="mission-foot-btn mission-foot-danger"
                disabled={busy}
                onClick={() => void onStopChat()}
              >
                Stop
              </button>
            </MutatorOnly>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmKill}
        title="Kill this agent?"
        body={`"${card.title}" will be terminated. This cannot be undone.`}
        confirmLabel="Kill"
        destructive
        onConfirm={() => void onKill()}
        onCancel={() => setConfirmKill(false)}
      />
    </article>
  );
}
