import { useEffect, useState } from 'preact/hooks';
import { signal } from '@preact/signals';
import {
  reviewGroups,
  reviewError,
  refreshReview,
  approveStaged,
  rejectStaged,
  sendBackStaged,
  editStaged,
  selectedBoardId,
  reviewFocusItemId,
  lastResumeOutcome,
} from '../../store/boards';
import { PromptDialog } from '../../components/ConfirmDialog';
import { MutatorOnly } from '../../components/MutatorOnly';
import type { StagedAction, StagedRecord } from '../../api/boards';

/**
 * The review queue (#588 Phase 6).
 *
 * The unit of review is the **staged action diff**, not a transcript. If a
 * decision needs the full agent log, the agent has not summarised well enough —
 * so a card carries the proposed writes, the reason, the evidence chips and a
 * deep link out to the real ticket, and nothing else.
 *
 * `needs_review` comes first because Mission Control puts `waiting` first for
 * the same reason: the group that needs a human is the one the page was opened
 * for. Grouping is done server-side so the mobile card list agrees with this.
 */

/** Per-item busy flag: one approval in flight must not grey out the queue. */
const busy = signal<Record<string, boolean>>({});

function setBusy(itemId: string, value: boolean) {
  busy.value = { ...busy.value, [itemId]: value };
}

type EditTarget = { record: StagedRecord; action: StagedAction };

export function ReviewPanel() {
  const boardId = selectedBoardId.value;
  const [editing, setEditing] = useState<EditTarget | null>(null);
  const [rejecting, setRejecting] = useState<StagedRecord | null>(null);
  const [sendingBack, setSendingBack] = useState<StagedRecord | null>(null);

  useEffect(() => {
    if (boardId) void refreshReview(boardId);
  }, [boardId]);

  if (!boardId) {
    return <p class="board-empty">Select a board to review its items.</p>;
  }

  const groups = reviewGroups.value;
  const open = groups.flatMap((g) => g.items).filter((r) => r.open).length;

  return (
    <section class="board-review">
      <header class="board-review-head">
        <h2>Review</h2>
        <p class="board-subtitle">
          {open === 0
            ? 'Nothing is waiting for you on this board.'
            : `${open} item${open === 1 ? '' : 's'} awaiting your decision.`}
        </p>
      </header>

      {reviewError.value && <p class="board-error">{reviewError.value}</p>}

      {groups.length === 0 && !reviewError.value && (
        <p class="board-empty">
          Nothing has been worked on this board yet. Start a run to have items
          picked up; in <strong>propose</strong> mode every write lands here
          first.
        </p>
      )}

      {groups.map((group) => (
        <section key={group.disposition} class="board-review-group">
          <h3 class={`board-disposition board-disposition-${group.disposition}`}>
            {group.disposition.replace(/_/g, ' ')}
            <span class="board-review-count mono">{group.count}</span>
          </h3>
          {group.items.map((record) => (
            <ReviewCard
              key={record.item_id}
              record={record}
              boardId={boardId}
              focused={reviewFocusItemId.value === record.item_id}
              onEdit={(action) => setEditing({ record, action })}
              onReject={() => setRejecting(record)}
              onSendBack={() => setSendingBack(record)}
            />
          ))}
        </section>
      ))}

      <PromptDialog
        open={editing !== null}
        title={editing ? `Edit ${editing.action.action}` : 'Edit'}
        body={
          'This replaces what the agent proposed. You still have to approve it ' +
          'afterwards — editing is not itself a decision.'
        }
        initial={
          editing
            ? String(editing.action.params.body ?? editing.action.preview ?? '')
            : ''
        }
        multiline
        confirmLabel="Save"
        onConfirm={async (value) => {
          if (!editing) return;
          const { record, action } = editing;
          setEditing(null);
          setBusy(record.item_id, true);
          await editStaged(boardId, record.item_id, action.id, {
            ...action.params,
            body: value,
          });
          setBusy(record.item_id, false);
        }}
        onCancel={() => setEditing(null)}
      />

      <PromptDialog
        open={rejecting !== null}
        title={
          rejecting
            ? `Reject ${rejecting.item_key || rejecting.item_id}`
            : 'Reject'
        }
        body="Nothing will be written to the board."
        placeholder="Why (the agent sees this)"
        confirmLabel="Reject"
        allowEmpty
        onConfirm={async (value) => {
          if (!rejecting) return;
          const record = rejecting;
          setRejecting(null);
          setBusy(record.item_id, true);
          await rejectStaged(boardId, record.item_id, value);
          setBusy(record.item_id, false);
        }}
        onCancel={() => setRejecting(null)}
      />

      {/* Send back REQUIRES a note, unlike reject. The agent is about to work
          this item again and the note is the only thing telling it what to
          change; a blank one looks like progress and is not. Refusing to act,
          by contrast, explains itself. */}
      <PromptDialog
        open={sendingBack !== null}
        title={
          sendingBack
            ? `Send back ${sendingBack.item_key || sendingBack.item_id}`
            : 'Send back'
        }
        body={
          'The agent works this again with your note. Nothing is written to ' +
          'the board, and its staged actions are discarded.'
        }
        placeholder="What should it do differently, or what do you need to know?"
        confirmLabel="Send back"
        multiline
        onConfirm={async (value) => {
          if (!sendingBack) return;
          const record = sendingBack;
          setSendingBack(null);
          setBusy(record.item_id, true);
          await sendBackStaged(boardId, record.item_id, value);
          setBusy(record.item_id, false);
        }}
        onCancel={() => setSendingBack(null)}
      />

      <ResumeNotice />
    </section>
  );
}

/**
 * What actually happened to a sent-back item.
 *
 * Shown rather than assumed, because the three tiers are genuinely different
 * and only one of them keeps the agent's own reasoning. Telling a reviewer
 * their context was preserved when it was not would make them trust the next
 * answer more than it deserves.
 */
function ResumeNotice() {
  const outcome = lastResumeOutcome.value;
  if (!outcome) return null;
  return (
    <p
      class={`board-resume-notice ${outcome.dispatched ? '' : 'is-warning'}`}
      role="status"
    >
      {outcome.detail}
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        onClick={() => {
          lastResumeOutcome.value = null;
        }}
      >
        Dismiss
      </button>
    </p>
  );
}

function ReviewCard({
  record,
  boardId,
  focused,
  onEdit,
  onReject,
  onSendBack,
}: {
  record: StagedRecord;
  boardId: string;
  focused: boolean;
  onEdit: (action: StagedAction) => void;
  onReject: () => void;
  onSendBack: () => void;
}) {
  const pending = record.pending_actions ?? [];
  const isBusy = busy.value[record.item_id] === true;
  const evidence = Object.entries(record.evidence ?? {});

  return (
    <article
      class={`board-review-card ${focused ? 'is-focused' : ''} ${
        record.open ? '' : 'is-decided'
      }`}
      data-item-id={record.item_id}
    >
      <header class="board-review-card-head">
        <span class="board-review-key mono">
          {record.item_key || record.item_id}
        </span>
        <span class="board-review-title">{record.item_title}</span>
        {/* Not optional: a reviewer will want to see the ticket natively
            before approving a customer-visible reply. */}
        {record.item_url && (
          <a
            class="board-review-link"
            href={record.item_url}
            target="_blank"
            rel="noreferrer noopener"
          >
            Open ticket ↗
          </a>
        )}
      </header>

      {pending.length > 0 && (
        <ul class="board-review-actions">
          {pending.map((action) => (
            <li key={action.id} class="board-review-action">
              <div class="board-review-action-head">
                <span class="board-review-action-name mono">
                  {action.action}
                </span>
                {action.edited && (
                  <span class="board-review-edited">edited by you</span>
                )}
                <MutatorOnly>
                  <button
                    type="button"
                    class="btn btn-ghost btn-sm"
                    disabled={isBusy}
                    onClick={() => onEdit(action)}
                  >
                    Edit
                  </button>
                </MutatorOnly>
              </div>
              {/* Verbatim <pre>, never markup. This text is about to be shown
                  to a customer, so what the reviewer reads has to be exactly
                  what gets sent. */}
              <pre class="board-review-preview">
                {action.preview || JSON.stringify(action.params, null, 2)}
              </pre>
            </li>
          ))}
        </ul>
      )}

      {record.reason && (
        <p class="board-review-reason">
          <strong>Why:</strong> {record.reason}
        </p>
      )}

      {evidence.length > 0 && (
        <ul class="board-evidence-chips">
          {evidence.map(([key, value]) => (
            <li key={key} class="board-evidence-chip">
              <span class="board-evidence-key">{key.replace(/_/g, ' ')}</span>
              <span class="mono">{String(value)}</span>
            </li>
          ))}
        </ul>
      )}

      {!record.open && (
        <p class="board-review-decided">
          {record.state}
          {record.decided_by ? ` · ${record.decided_by}` : ''}
          {record.state === 'partial' && (
            <span class="board-review-partial">
              {' '}
              — some writes failed; read the results before retrying
            </span>
          )}
        </p>
      )}

      {record.open && (
        <MutatorOnly>
          <footer class="board-review-card-foot">
            <button
              type="button"
              class="btn btn-primary btn-sm"
              disabled={isBusy || pending.length === 0}
              onClick={async () => {
                setBusy(record.item_id, true);
                await approveStaged(boardId, record);
                setBusy(record.item_id, false);
              }}
            >
              {pending.length <= 1 ? 'Approve' : `Approve ${pending.length}`}
            </button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              disabled={isBusy}
              onClick={onReject}
            >
              Reject
            </button>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              disabled={isBusy}
              onClick={onSendBack}
            >
              Send back
            </button>
          </footer>
        </MutatorOnly>
      )}
    </article>
  );
}
