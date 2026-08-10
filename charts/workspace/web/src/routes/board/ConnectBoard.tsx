import { useEffect, useState } from 'preact/hooks';
import { Modal } from '../../components/Modal';
import {
  boardTemplates,
  connectBoard,
  refreshTemplates,
  selectBoard,
  templatesError,
  type ConnectOutcome,
} from '../../store/boards';
import type { BoardTemplate } from '../../api/boards';

/**
 * Connect a board from a starter template.
 *
 * Before this existed the empty state said "ask an agent to build one for
 * you", which is a fine answer for a tracker nobody has written a connector
 * for and a poor one for the three that ship with the product. Someone who
 * wants their Zendesk queue read should not have to commission a JSON author.
 *
 * The form is generated from the template's own `placeholders` and
 * `credential` blocks rather than switch-cased per vendor, so a fourth
 * template is a Python change and nothing here. Substitution happens on the
 * server for a reason worth keeping in mind while editing this file: these
 * values land inside URL paths and inside Jira's JQL string, so a value with a
 * space in it is not a validation nicety — it is a query against a different
 * board.
 *
 * The last step is always `test-fetch`. A board that has been saved has
 * demonstrated nothing; a board that has fetched has. The dialog reports which
 * of the two happened and never conflates them.
 */
export function ConnectBoard({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [templateId, setTemplateId] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [secret, setSecret] = useState('');
  const [username, setUsername] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<ConnectOutcome | null>(null);

  useEffect(() => {
    if (open) void refreshTemplates();
  }, [open]);

  // A fresh dialog every time it opens. Reopening to a half-filled form from
  // a previous attempt is the kind of state that gets a token pasted into the
  // wrong board.
  useEffect(() => {
    if (!open) return;
    setTemplateId('');
    setValues({});
    setSecret('');
    setUsername('');
    setError(null);
    setOutcome(null);
  }, [open]);

  const list = boardTemplates.value;
  const template: BoardTemplate | undefined = list.find((t) => t.id === templateId);
  const cred = template?.credential ?? null;

  const ready =
    !!template &&
    template.placeholders.every((p) => (values[p.token] ?? '').trim()) &&
    (!cred || secret.trim().length > 0) &&
    (!cred || cred.format !== 'basic' || username.trim().length > 0);

  async function submit(e: Event) {
    e.preventDefault();
    if (!template || busy) return;
    setBusy(true);
    setError(null);
    const res = await connectBoard({
      templateId: template.id,
      values,
      credential: cred
        ? {
            name: cred.name,
            secret: secret.trim(),
            format: cred.format,
            username: username.trim(),
          }
        : undefined,
    });
    setBusy(false);
    // The secret leaves component state the moment it has been sent, whether
    // or not the rest succeeded. A retry re-types it; that is the cheaper
    // mistake.
    setSecret('');
    if (res.error) {
      setError(res.error);
      return;
    }
    setOutcome(res.outcome);
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      labelledBy="connect-board-title"
      width={560}
      class="board-connect"
    >
      <h2 id="connect-board-title">Connect a board</h2>

      {outcome ? (
        <Result outcome={outcome} onClose={onClose} />
      ) : (
        <form onSubmit={submit}>
          <p class="board-subtitle">
            Start from a template, fill in what it cannot know about you, and
            it fetches once to prove it works.
          </p>

          {templatesError.value && (
            <p class="board-error">{templatesError.value}</p>
          )}

          <div class="board-connect-picker" role="radiogroup" aria-label="Template">
            {list.map((t) => (
              <button
                key={t.id}
                type="button"
                role="radio"
                aria-checked={templateId === t.id}
                class={`board-connect-choice ${templateId === t.id ? 'is-active' : ''}`}
                onClick={() => {
                  setTemplateId(t.id);
                  setValues({});
                  setSecret('');
                  setUsername('');
                  setError(null);
                }}
              >
                <span class="board-connect-choice-name">{t.display_name}</span>
                <span class="board-vendor">{t.vendor}</span>
              </button>
            ))}
            {list.length === 0 && (
              <p class="board-empty">No templates available.</p>
            )}
          </div>

          {template && (
            <>
              {template.placeholders.map((p) => (
                <label key={p.token} class="board-run-field">
                  <span>{p.label}</span>
                  <input
                    value={values[p.token] ?? ''}
                    placeholder={p.example}
                    autocomplete="off"
                    onInput={(e) =>
                      setValues({
                        ...values,
                        [p.token]: (e.target as HTMLInputElement).value,
                      })
                    }
                  />
                  <small class="board-connect-help">{p.help}</small>
                </label>
              ))}

              {cred ? (
                <>
                  {cred.format === 'basic' && (
                    <label class="board-run-field">
                      <span>{cred.username_label}</span>
                      <input
                        value={username}
                        placeholder="you@example.com"
                        autocomplete="off"
                        onInput={(e) =>
                          setUsername((e.target as HTMLInputElement).value)
                        }
                      />
                    </label>
                  )}
                  <label class="board-run-field">
                    <span>{cred.secret_label}</span>
                    <input
                      type="password"
                      value={secret}
                      autocomplete="off"
                      placeholder="paste the raw token"
                      onInput={(e) =>
                        setSecret((e.target as HTMLInputElement).value)
                      }
                    />
                    <small class="board-connect-help">{cred.help}</small>
                  </label>
                  <p class="board-cred-note">
                    Stored as <code class="mono">{cred.name}</code> in the board
                    credential store. The agent never sees it — the connector
                    names it and the server resolves it when it makes the
                    request.
                  </p>
                </>
              ) : (
                <p class="board-cred-note">
                  Nothing to paste — this template uses the workspace's own
                  GitHub App token, which is already brokered for you.
                </p>
              )}

              <details class="board-connect-steps">
                <summary>Setup steps for {template.display_name}</summary>
                <ol>
                  {template.needs.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ol>
              </details>
            </>
          )}

          {error && <p class="board-error">{error}</p>}

          <div class="board-connect-actions">
            <button type="button" class="btn btn-ghost btn-sm" onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              class="btn btn-primary btn-sm"
              disabled={!ready || busy}
            >
              {busy ? 'Connecting…' : 'Connect and fetch'}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}

/**
 * What actually happened, in the two cases that are genuinely different.
 *
 * A created board whose first fetch failed is NOT deleted. The connector is
 * usually right and the credential wrong, and discarding it would make the
 * operator retype every answer to discover which. It stays, visibly unproven,
 * with the vendor's own error next to it.
 */
function Result({
  outcome,
  onClose,
}: {
  outcome: ConnectOutcome;
  onClose: () => void;
}) {
  const ok = !outcome.fetchError;
  return (
    <div class="board-connect-result">
      {ok ? (
        <>
          <p class="board-connect-ok">
            Connected. It fetched <strong>{outcome.fetched}</strong>{' '}
            {outcome.fetched === 1 ? 'item' : 'items'}
            {outcome.complete ? '' : ' (listing incomplete)'}.
          </p>
          <p class="board-subtitle">
            That is a real read through the same engine a run uses, so the
            connector has demonstrated it works rather than been asserted to.
            Writes are a separate question: nothing has been written, and in
            propose mode nothing will be until you approve it.
          </p>
        </>
      ) : (
        <>
          <p class="board-error">
            The board was created but its first fetch failed:{' '}
            {outcome.fetchError}
          </p>
          <p class="board-subtitle">
            It is kept, not discarded — the connector is usually right and the
            credential wrong. Fix the credential and use Test fetch, rather
            than filling this in again.
          </p>
        </>
      )}
      <div class="board-connect-actions">
        <button
          type="button"
          class="btn btn-primary btn-sm"
          onClick={() => {
            void selectBoard(outcome.boardId);
            onClose();
          }}
        >
          Open {outcome.boardId}
        </button>
      </div>
    </div>
  );
}
