import { useEffect, useState } from 'preact/hooks';
import {
  missionCards,
  missionPulse,
  refreshMission,
  startMissionPolling,
  stopMissionPolling,
} from '../../store/mission';
import type { MissionCard, MissionPromptOption, MissionState } from '../../api/mission';
import { sendFollowup } from '../../store/tasks';
import { navigate } from '../../store/router';
import { Icon } from '../../components/Icon';
import { MutatorOnly } from '../../components/MutatorOnly';
import { MissionDrawer } from '../mission/MissionDrawer';
import { rel } from '../mission/MissionCard';
import { DesktopSection } from './DesktopSection';
// The detail drawer + quick replies reuse the board's styles.
import '../mission/mission.css';

/**
 * Condensed Mission Control strip on the Desktop (#433) — replaces the old
 * passive Activity feed. A pulse row (running / waiting / done-today) over
 * the top few live cards from /api/missioncontrol/queue, waiting-on-you
 * first. Reuses store/mission's SSE-plus-fallback subscription, so the strip
 * is live without its own polling loop. Clicking a card opens the existing
 * MissionDrawer; "View all" jumps to the full board. Self-hides while the
 * queue is empty so a first-run Desktop is just composer + dock.
 */
const MAX_CARDS = 4;

const STATE_LABEL: Record<MissionState, string> = {
  waiting: 'Waiting',
  running: 'Running',
  review: 'Review',
  done: 'Done',
};

const KIND_LABEL: Record<MissionCard['kind'], string> = {
  build: 'BUILD',
  chat: 'CHAT',
  subagent: 'SUB-AGENT',
};

/** State-appropriate age, mirroring the board card's semantics. */
function timeLabel(card: MissionCard): string {
  const now = Date.now() / 1000;
  if (card.state === 'running') {
    return card.created_at ? rel(now - card.created_at) : '';
  }
  if (card.state === 'waiting') {
    const since = card.waiting_since ?? card.updated_at;
    return since ? `${rel(now - since)} waiting` : '';
  }
  const at = card.finished_at ?? card.updated_at;
  return at ? `${rel(now - at)} ago` : '';
}

function MiniCard({
  card,
  onSelect,
}: {
  card: MissionCard;
  onSelect: (id: string) => void;
}) {
  const [busy, setBusy] = useState(false);

  // Same wire format as the board's quick replies: forward the option key so
  // tmux sends the keystroke to the waiting TUI.
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
      class={`dt-mc-card dt-mc-card-${card.state}`}
      onClick={() => onSelect(card.id)}
      data-card-id={card.id}
    >
      <div class="dt-mc-top">
        <span class={`dt-mc-state dt-mc-state-${card.state}`}>
          <span class="dt-mc-dot" aria-hidden="true" />
          {STATE_LABEL[card.state]}
        </span>
        <span class="dt-mc-kind mono">{KIND_LABEL[card.kind]}</span>
        <span class="dt-mc-time muted">{timeLabel(card)}</span>
      </div>
      <div class="dt-mc-title">{card.title}</div>
      {card.headline && <div class="dt-mc-headline muted">{card.headline}</div>}
      {card.state === 'waiting' && card.waiting_prompt && (
        <div class="dt-mc-prompt" onClick={(e) => e.stopPropagation()}>
          {card.waiting_prompt.question && (
            <div class="dt-mc-q">{card.waiting_prompt.question}</div>
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
    </article>
  );
}

export function DesktopMissionStrip() {
  // Namespaced id of the card open in the detail drawer (null = closed).
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    startMissionPolling(10000);
    return () => stopMissionPolling();
  }, []);

  const pulse = missionPulse.value;
  // The queue arrives pre-sorted waiting → running → review → done, newest
  // first within each group — exactly the strip's priority order.
  const cards = missionCards.value.slice(0, MAX_CARDS);

  // Self-hide (rather than an empty card) while there is nothing in flight.
  if (cards.length === 0) return null;

  return (
    <DesktopSection
      class="dt-section-mission"
      title="Mission Control"
      icon={<Icon name="mission" size={13} />}
      meta={
        <button type="button" class="dt-mc-viewall" onClick={() => navigate('/mission')}>
          View all <Icon name="chevron-right" size={11} />
        </button>
      }
      data-dt-stop="true"
      aria-label="Mission Control"
    >
      {pulse && (
        <div class="dt-mc-pulse">
          <span class={pulse.running > 0 ? 'dt-mc-pulse-live' : ''}>
            <b>{pulse.running}</b> running
          </span>
          <span class={pulse.waiting > 0 ? 'dt-mc-pulse-warn' : ''}>
            <b>{pulse.waiting}</b> waiting on you
          </span>
          <span>
            <b>{pulse.done_today}</b> done today
          </span>
        </div>
      )}
      <div class="dt-mc-cards">
        {cards.map((c) => (
          <MiniCard key={c.id} card={c} onSelect={setSelectedId} />
        ))}
      </div>

      <MissionDrawer cardId={selectedId} onClose={() => setSelectedId(null)} />
    </DesktopSection>
  );
}
