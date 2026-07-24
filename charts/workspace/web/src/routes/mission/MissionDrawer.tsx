import { useEffect, useState } from 'preact/hooks';
import {
  getMissionCardDetail,
  type MissionDetail,
  type MissionTimelineEntry,
} from '../../api/mission';
import { sendThreadMessage } from '../../api/hypervisor';
import { sendFollowup } from '../../store/tasks';
import { refreshMission } from '../../store/mission';
import { navigate } from '../../store/router';
import { pushToast } from '../../store/ui';
import { Drawer } from '../../components/Drawer';
import { Button } from '../../components/primitives/Button';
import { MutatorOnly } from '../../components/MutatorOnly';
import { rel, cardHref, EvidenceChips } from './MissionCard';

// Live cards keep moving while the drawer is open — refresh on this cadence.
const DETAIL_REFRESH_MS = 8000;

/** Dot tone for a timeline entry, keyed off the server's status field. */
function entryTone(e: MissionTimelineEntry): string {
  if (e.status === 'error') return 'error';
  if (e.status === 'pending') return 'pending';
  if (e.status === 'muted' || e.kind === 'status') return 'muted';
  return 'ok';
}

/**
 * Detail drawer for one board card (#425 phase 3): activity timeline,
 * bounded output tail, and a follow-up composer so any agent can be steered
 * without leaving the board. Selecting a card opens it; Escape/scrim closes.
 */
export function MissionDrawer({
  cardId,
  onClose,
}: {
  cardId: string | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<MissionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);

  useEffect(() => {
    setDetail(null);
    setError(null);
    setMessage('');
    if (!cardId) return;
    let alive = true;
    const load = async () => {
      try {
        const d = await getMissionCardDetail(cardId);
        if (!alive) return;
        setDetail(d);
        setError(null);
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      }
    };
    void load();
    const timer = setInterval(() => void load(), DETAIL_REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [cardId]);

  const card = detail?.card ?? null;
  const live = card != null && (card.state === 'running' || card.state === 'waiting');
  // Follow-ups reach a task's tmux session only while it's alive; idle chats
  // stay messageable — sending resumes the thread.
  const canCompose = card != null && (card.kind === 'chat' || live);
  const now = Date.now() / 1000;

  async function onSend() {
    const text = message.trim();
    if (!card || !text || sending) return;
    setSending(true);
    try {
      if (card.kind === 'chat') {
        await sendThreadMessage(card.ref_id, text);
        pushToast('Message sent', { kind: 'success' });
      } else {
        // sendFollowup (store) already toasts success/failure.
        await sendFollowup(card.ref_id, text);
      }
      setMessage('');
      await refreshMission();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Send failed', { kind: 'danger' });
    } finally {
      setSending(false);
    }
  }

  return (
    <Drawer open={cardId != null} onClose={onClose} title={card?.title ?? 'Agent detail'}>
      {error && <div class="mission-error" role="alert">{error}</div>}
      {!error && !detail && <div class="muted">Loading…</div>}

      {card && detail && (
        <div class="mission-drawer">
          <div class="mission-drawer-meta">
            <span class="mission-kind">{card.kind.toUpperCase()}</span>
            <span class={`mission-drawer-state mission-drawer-state-${card.state}`}>
              {card.state}
            </span>
            {(card.assistant || card.model) && (
              <span class="mission-agent">
                {card.assistant}
                {card.assistant && card.model ? ' · ' : ''}
                {card.model}
              </span>
            )}
          </div>
          {card.headline && <p class="mission-drawer-headline muted">{card.headline}</p>}
          <EvidenceChips evidence={card.evidence} />
          {(card.repo || card.branch) && (
            <div class="mission-card-meta">
              {card.repo && <span>{card.repo}</span>}
              {card.branch && <span class="mission-branch">⎇ {card.branch}</span>}
            </div>
          )}

          <h3 class="mission-drawer-h">Timeline</h3>
          <ol class="mission-timeline">
            {detail.timeline.map((e, i) => (
              <li key={i} class="mission-timeline-row">
                <span
                  class={`mission-timeline-dot mission-timeline-dot-${entryTone(e)}`}
                  aria-hidden="true"
                />
                <div class="mission-timeline-body">
                  <div class="mission-timeline-text">
                    {e.link ? (
                      <button
                        type="button"
                        class="mission-lineage-link"
                        onClick={() => navigate(cardHref(e.link as string))}
                      >
                        {e.text}
                      </button>
                    ) : (
                      e.text
                    )}
                    {e.at != null && (
                      <span class="mission-timeline-at muted">{rel(now - e.at)} ago</span>
                    )}
                  </div>
                  {e.detail && <div class="mission-timeline-detail muted">{e.detail}</div>}
                </div>
              </li>
            ))}
          </ol>

          {detail.output_tail && (
            <>
              <h3 class="mission-drawer-h">Recent output</h3>
              <pre class="mission-tail">{detail.output_tail}</pre>
            </>
          )}

          {canCompose && (
            <MutatorOnly>
              <div class="mission-composer">
                <textarea
                  class="mission-composer-input"
                  rows={2}
                  placeholder={
                    card.kind === 'chat' ? 'Send a message…' : 'Send a follow-up…'
                  }
                  value={message}
                  disabled={sending}
                  aria-label="Follow-up message"
                  onInput={(e) => setMessage((e.target as HTMLTextAreaElement).value)}
                />
                <Button
                  size="sm"
                  variant="primary"
                  disabled={sending || !message.trim()}
                  onClick={() => void onSend()}
                >
                  Send
                </Button>
              </div>
            </MutatorOnly>
          )}

          <div class="mission-drawer-foot">
            <Button size="sm" onClick={() => navigate(cardHref(card.id))}>
              Open full session
            </Button>
          </div>
        </div>
      )}
    </Drawer>
  );
}
