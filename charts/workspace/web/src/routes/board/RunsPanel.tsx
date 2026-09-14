import { useEffect, useState } from 'preact/hooks';
import {
  selectedBoard,
  selectedBoardId,
  selectedBoardRuns,
  activeRun,
  runsError,
  refreshRuns,
  openRun,
  closeRun,
  startRun,
  stopRun,
  strategies,
  strategyPreview,
  refreshStrategies,
  previewStrategy,
  boardMetrics,
  refreshBoardMetrics,
  runFormFor,
  setRunForm,
} from '../../store/boards';
import {
  clampLabel,
  truncationLabel,
  runItemStateLabel,
  runItemOrder,
  isRunItemLive,
  dispositionLabel,
  elapsedLabel,
} from '../../api/boards';
import { MutatorOnly } from '../../components/MutatorOnly';
import type { BoardRun, BoardRunSummary } from '../../api/boards';

/**
 * Runs (#588 Phase 4/6) — start N items working in parallel and watch them.
 *
 * Two things are shown that a naive progress bar would hide, and both are the
 * whole reason this panel exists rather than a spinner:
 *
 * - **`clamp_reason`** — when the pod was busy, the run got fewer workers than
 *   were asked for. Left unsaid, that reads as "it was slow"; said, it reads as
 *   "raise KC_MAX_TASKS or wait".
 * - **an incomplete listing** — "we worked every open ticket" and "every one we
 *   could see" are different claims, and only one of them is true when the
 *   board truncated the read.
 */
export function RunsPanel() {
  const boardId = selectedBoardId.value;
  // Every choice on this form lives in the store, per board (#643). Leaving
  // the Runs tab unmounts this panel, and watching a run means bouncing to
  // Review and back — with the values in useState, each trip quietly reset
  // Items / At once to the heavier defaults under an operator who had
  // deliberately turned them down.
  const { mode, limit, concurrency, strategy } = runFormFor(boardId);
  const [starting, setStarting] = useState(false);
  const [previewing, setPreviewing] = useState(false);

  // Progress polling is NOT started here. It belongs to the route, which stays
  // mounted across tab switches — owning it from this panel meant stepping
  // over to Review to approve something silently stopped the poll tracking the
  // run you went there to act on.
  useEffect(() => {
    if (!boardId) return;
    void refreshRuns(boardId);
    void refreshStrategies(boardId);
    strategyPreview.value = null;
  }, [boardId]);

  // A restored strategy that the board no longer defines would leave the
  // select showing nothing while still filtering the run. Drop it once the
  // real list has loaded.
  const knownStrategies = strategies.value;
  useEffect(() => {
    if (!boardId || !strategy) return;
    if (!Object.keys(knownStrategies).length) return;
    if (!(strategy in knownStrategies)) setRunForm(boardId, { strategy: '' });
  }, [boardId, strategy, knownStrategies]);

  /** The selection this form will actually send. A saved strategy supplies
   *  the filters; the two numbers on the form always win, because they are
   *  what the person is looking at. */
  function currentSelect() {
    const base = strategy ? strategies.value[strategy] ?? {} : {};
    return { order: 'updated_at asc', ...base, limit };
  }

  if (!boardId) {
    return <p class="board-empty">Select a board to run it.</p>;
  }

  const runs = selectedBoardRuns.value;
  const open = activeRun.value;

  return (
    <section class="board-runs">
      <header class="board-review-head">
        <h2>Runs</h2>
        <p class="board-subtitle">
          Work several items at once. Re-running the same board skips anything
          already processed — an item somebody edited comes back.
        </p>
      </header>

      {runsError.value && <p class="board-error">{runsError.value}</p>}

      <MutatorOnly>
        <form
          class="board-run-form"
          onSubmit={async (e) => {
            e.preventDefault();
            setStarting(true);
            await startRun(boardId, {
              mode,
              concurrency,
              select: currentSelect(),
              stop_on: { consecutive_failures: 3 },
            });
            strategyPreview.value = null;
            setStarting(false);
          }}
        >
          <label class="board-run-field">
            <span>Selection</span>
            <select
              value={strategy}
              aria-label="Selection strategy"
              onInput={(e) => {
                setRunForm(boardId, {
                  strategy: (e.target as HTMLSelectElement).value,
                });
                strategyPreview.value = null;
              }}
            >
              <option value="">Everything not yet processed</option>
              {Object.keys(strategies.value).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label class="board-run-field">
            <span>Mode</span>
            <select
              value={mode}
              aria-label="Run mode"
              onInput={(e) =>
                setRunForm(boardId, {
                  mode: (e.target as HTMLSelectElement).value as
                    | 'propose'
                    | 'autonomous',
                })
              }
            >
              <option value="propose">Propose — stage every write</option>
              <option value="autonomous">Autonomous — write directly</option>
            </select>
          </label>
          <label class="board-run-field">
            <span>Items</span>
            <input
              type="number"
              min={1}
              max={500}
              value={limit}
              onInput={(e) =>
                setRunForm(boardId, {
                  limit: Number((e.target as HTMLInputElement).value) || 1,
                })
              }
            />
          </label>
          <label class="board-run-field">
            <span>At once</span>
            <input
              type="number"
              min={1}
              max={8}
              value={concurrency}
              onInput={(e) =>
                setRunForm(boardId, {
                  concurrency: Number((e.target as HTMLInputElement).value) || 1,
                })
              }
            />
          </label>
          <button
            type="submit"
            class="btn btn-primary btn-sm"
            disabled={starting || selectedBoard.value?.credential_set === false}
          >
            {starting ? 'Starting…' : 'Start run'}
          </button>
          {/* One vendor listing, so it is a deliberate click rather than a
              reaction to every keystroke — a preview per keypress would spend
              the board's rate-limit budget on a form nobody has submitted. */}
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            disabled={previewing || selectedBoard.value?.credential_set === false}
            onClick={async () => {
              setPreviewing(true);
              await previewStrategy(boardId, currentSelect());
              setPreviewing(false);
            }}
          >
            {previewing ? 'Checking…' : 'What would this work?'}
          </button>
          {mode === 'autonomous' && (
            <p class="board-run-warn">
              Autonomous writes to the board without asking. Use propose until
              you have watched a run end to end.
            </p>
          )}
        </form>
      </MutatorOnly>

      <PreviewLine />
      <ApprovalRate />

      {runs.length === 0 && <p class="board-empty">No runs yet.</p>}

      <ul class="board-run-list">
        {runs.map((run) => (
          <li key={run.id}>
            <button
              type="button"
              class={`board-run-row ${open?.id === run.id ? 'is-active' : ''}`}
              onClick={() =>
                open?.id === run.id ? closeRun() : void openRun(boardId, run.id)
              }
            >
              <RunLine run={run} />
            </button>

            {open?.id === run.id && (
              <div class="board-run-detail">
                {clampLabel(run) && (
                  <p class="board-run-clamp">{clampLabel(run)}</p>
                )}
                {!run.listing_complete && (
                  <p class="board-warn">
                    {truncationLabel({
                      complete: run.listing_complete,
                      truncation_reason: run.truncation_reason,
                    })}
                  </p>
                )}
                {run.error && <p class="board-error">{run.error}</p>}
                <RunTally run={run} />
                <RunItemTable run={open} />
                {run.status === 'running' && (
                  <MutatorOnly>
                    <button
                      type="button"
                      class="btn btn-ghost btn-sm"
                      onClick={() => void stopRun(boardId, run.id)}
                    >
                      Stop
                    </button>
                    <span class="board-run-stop-note">
                      Items already dispatched will finish — stopping a write
                      halfway leaves a half-applied change.
                    </span>
                  </MutatorOnly>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * "This would work 7 items and skip 12 already processed."
 *
 * The alternative to knowing is spending twenty agents to find out, and the
 * skip count is the sentence that makes a re-run understandable rather than
 * looking like the board is broken.
 */
function PreviewLine() {
  const p = strategyPreview.value;
  if (!p) return null;
  return (
    <div class="board-preview">
      <p class="board-preview-head">
        Would work <strong>{p.would_work}</strong> of {p.matched} matching
        {p.skipped_already_processed > 0 && (
          <> · skipping {p.skipped_already_processed} already processed</>
        )}
        {p.held_by_another_run > 0 && (
          <> · {p.held_by_another_run} held by another run</>
        )}
      </p>
      {!p.listing_complete && (
        <p class="board-warn">
          The board listing was incomplete ({p.truncation_reason}) — there may
          be more items than this beyond what we could read.
        </p>
      )}
      {p.sample.length > 0 && (
        <ul class="board-preview-sample">
          {p.sample.map((i) => (
            <li key={i.id}>
              <span class="mono">{i.key}</span> {i.title}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Approval rate per board (#588 Phase 7).
 *
 * The issue's own reading: *100% suggests a candidate for autonomous mode;
 * 40% suggests a prompt problem.* Nothing is shown until a human has actually
 * decided something — a 0% over zero decisions would be a lie in the shape of
 * a statistic.
 */
function ApprovalRate() {
  const boardId = selectedBoardId.value;
  const m = boardMetrics.value;

  useEffect(() => {
    if (boardId) void refreshBoardMetrics(boardId);
  }, [boardId]);

  if (!m || m.approval_rate === null || m.approval_rate === undefined) {
    return null;
  }
  const pct = Math.round(m.approval_rate * 100);
  // `dispositions` may be absent on a degraded response; treat that as "no
  // distribution to judge" rather than letting the whole panel throw. An
  // approval rate is still worth showing without it.
  const dist = m.dispositions ?? {};
  const total = Object.values(dist).reduce((a, b) => a + b, 0);
  const inflated = total > 0 && (dist.needs_rescoping ?? 0) > total / 2;
  return (
    <p class="board-approval-rate">
      <strong>{pct}%</strong> of {m.decided} decisions approved
      {pct === 100 && <> — this board may be a candidate for autonomous mode</>}
      {pct <= 50 && (
        <> — a low rate usually means a prompt problem, not a board problem</>
      )}
      {inflated && (
        <span class="board-warn">
          {' '}More than half of all items came back as “needs rescoping”. That
          is usually the agent finding it the easy answer rather than the board
          getting vaguer.
        </span>
      )}
    </p>
  );
}

function RunLine({ run }: { run: BoardRunSummary }) {
  const settled = run.done + run.failed + run.skipped;
  return (
    <>
      <span class={`board-run-status board-run-status-${run.status}`}>
        {run.status}
      </span>
      <span class="board-run-mode">{run.mode}</span>
      <span class="board-run-progress mono">
        {settled}/{run.total}
      </span>
      {run.failed > 0 && (
        <span class="board-run-failed mono">{run.failed} failed</span>
      )}
      {clampLabel(run) && (
        <span class="board-run-clamped" title={run.clamp_reason}>
          clamped
        </span>
      )}
      {!run.listing_complete && (
        <span class="board-run-partial" title={run.truncation_reason}>
          partial listing
        </span>
      )}
    </>
  );
}

/**
 * A run's own counts, above its item list.
 *
 * `4/9` in the collapsed row says how much is finished but not what the rest
 * is doing, and "3 queued behind 1 worker" and "3 failing right now" are very
 * different situations that the single fraction renders identically. Only
 * non-zero buckets are drawn, so a clean run stays a short line.
 */
function RunTally({ run }: { run: BoardRunSummary }) {
  const c = run.counts ?? {};
  const buckets: { key: string; label: string; n: number }[] = [
    { key: 'working', label: 'working', n: (c.working ?? 0) + (c.claimed ?? 0) },
    { key: 'pending', label: 'queued', n: c.pending ?? 0 },
    { key: 'done', label: 'done', n: c.done ?? 0 },
    { key: 'failed', label: 'failed', n: c.failed ?? 0 },
    { key: 'skipped', label: 'skipped', n: c.skipped ?? 0 },
  ].filter((b) => b.n > 0);

  if (buckets.length === 0) return null;

  return (
    <ul class="board-run-tally" aria-label="Run progress by state">
      {buckets.map((b) => (
        <li key={b.key} class={`board-run-tally-${b.key}`}>
          <span class="mono">{b.n}</span> {b.label}
        </li>
      ))}
    </ul>
  );
}

/**
 * The items of one run, live work first.
 *
 * Two things were previously left for the reader to work out. The state was
 * rendered as its raw enum in the same ink as everything else, so `pending`,
 * `working` and `done` were three words of identical weight — you had to read
 * each row to find the one that was moving. And the rows came out in map
 * insertion order, which interleaves finished items between running ones.
 *
 * Sorting live work to the top and giving each state a pill means the answer
 * to "what is happening right now" is the top of the table, every time.
 */
function RunItemTable({ run }: { run: BoardRun }) {
  // Elapsed times only tick while something is actually live; a settled run
  // must not hold a timer open for a table nobody is watching change.
  const items = Object.values(run.items ?? {});
  const anyLive = items.some((i) => isRunItemLive(i.state));
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!anyLive) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [anyLive]);

  if (items.length === 0) {
    return <p class="board-empty">This run has no items.</p>;
  }

  const sorted = [...items].sort((a, b) => {
    const byState = runItemOrder(a.state) - runItemOrder(b.state);
    if (byState !== 0) return byState;
    return (a.key || a.id).localeCompare(b.key || b.id, undefined, {
      numeric: true,
    });
  });

  return (
    <table class="board-run-items">
      <thead class="board-run-items-head">
        <tr>
          <th scope="col">Item</th>
          <th scope="col">Title</th>
          <th scope="col">State</th>
          <th scope="col">Outcome</th>
        </tr>
      </thead>
      <tbody>
        {sorted.map((item) => {
          const live = isRunItemLive(item.state);
          return (
            <tr key={item.id} class={`board-run-item-${item.state}`}>
              <td class="mono">{item.key || item.id}</td>
              <td class="board-run-item-title">{item.title}</td>
              <td>
                <span class={`board-state-pill board-state-${item.state}`}>
                  {item.state === 'working' && (
                    <span class="board-spinner" aria-hidden="true" />
                  )}
                  {runItemStateLabel(item.state)}
                </span>
                {/* Elapsed only while it is live: on a settled item the
                    number would keep climbing forever and mean nothing. */}
                {live && item.updated_at > 0 && (
                  <span class="board-run-item-elapsed mono">
                    {elapsedLabel(item.updated_at, now)}
                  </span>
                )}
              </td>
              <td>
                {/* A `failed` disposition next to a FAILED state pill says the
                    same word twice; the error underneath is the part that
                    carries information. */}
                {item.disposition && item.disposition !== item.state && (
                  <span
                    class={`board-disposition board-disposition-${item.disposition}`}
                  >
                    {dispositionLabel(item.disposition)}
                  </span>
                )}
                {item.error && (
                  <span class="board-run-item-error">{item.error}</span>
                )}
                {!item.disposition && !item.error && live && (
                  <span class="board-run-item-waiting">—</span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
