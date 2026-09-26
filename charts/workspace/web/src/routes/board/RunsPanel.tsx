import { useEffect, useState } from 'preact/hooks';
import {
  hasLiveRun,
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
  reviewGroups,
  reviewBoardId,
  openReviewFor,
} from '../../store/boards';
import {
  clampLabel,
  truncationLabel,
  runItemStateLabel,
  runItemOrder,
  isRunItemLive,
  dispositionLabel,
} from '../../api/boards';
import { MutatorOnly } from '../../components/MutatorOnly';
import { navigate, routeHref } from '../../store/router';
import type { BoardRun, BoardRunItem, BoardRunSummary } from '../../api/boards';
import { listWorkdirs, type WorkdirOption } from '../../api/tasks';
import { shouldWarnSharedTree } from '../../util/worktree';

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
  const { mode, limit, concurrency, strategy, workdir, isolate } = runFormFor(boardId);
  const [starting, setStarting] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  // Git folders a run can point its agents at (#701). A repository is
  // optional: without one the run is tracker-only, exactly as before.
  const [dirs, setDirs] = useState<WorkdirOption[]>([]);
  useEffect(() => {
    listWorkdirs()
      .then((d) => setDirs(Array.isArray(d) ? d : []))
      .catch(() => setDirs([]));
  }, []);
  const gitDirs = dirs.filter((d) => d.is_git_repo);
  const sharedTree = shouldWarnSharedTree(workdir, isolate, concurrency);

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
  // A second run on a board that already has one in flight is not a parallel
  // run: item leases are per BOARD, so it can only mark as `skipped`
  // everything the live run holds, and it reports that as a page of skips
  // (#712). The server refuses it with a 409; the form says so first, because
  // a disabled button with a reason beats an error after the click. The same
  // sentence is the button's tooltip.
  const blockedByRun = hasLiveRun.value
    ? 'A run is already in flight on this board. Stop it, or wait for it to '
      + 'finish — a second run could only skip the items this one holds.'
    : '';
  const needsCredential = selectedBoard.value?.credential_set === false;

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
              ...(workdir ? { workdir, isolate } : {}),
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
          <label class="board-run-field">
            <span>Repository</span>
            <select
              value={workdir}
              aria-label="Repository"
              onInput={(e) => {
                const value = (e.target as HTMLSelectElement).value;
                // Picking a repository turns isolation on: several agents in
                // one checkout is the collision this exists to prevent.
                setRunForm(boardId, value ? { workdir: value, isolate: true } : { workdir: '' });
              }}
            >
              <option value="">None — tracker only</option>
              {gitDirs.map((d) => (
                <option key={d.path} value={d.path}>
                  {d.label ?? d.path}
                </option>
              ))}
              {workdir && !gitDirs.some((d) => d.path === workdir) && (
                <option value={workdir}>{workdir}</option>
              )}
            </select>
          </label>
          {workdir && (
            <label class="board-run-check">
              <input
                type="checkbox"
                checked={isolate}
                onChange={(e) =>
                  setRunForm(boardId, { isolate: (e.target as HTMLInputElement).checked })
                }
              />
              Isolated worktree per item
            </label>
          )}
          <button
            type="submit"
            class="btn btn-primary btn-sm"
            disabled={starting || needsCredential || !!blockedByRun}
            title={
              blockedByRun ||
              (needsCredential
                ? 'This board has no credential, so a run could not authenticate.'
                : undefined)
            }
          >
            {starting ? 'Starting…' : blockedByRun ? 'Run in progress' : 'Start run'}
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
          {/* Said in words as well as in the disabled state: a greyed button
              with no explanation reads as a broken page. */}
          {blockedByRun && (
            <p class="board-run-warn" role="status">
              {blockedByRun}
            </p>
          )}
          {mode === 'autonomous' && (
            <p class="board-run-warn">
              Autonomous writes to the board without asking. Use propose until
              you have watched a run end to end.
            </p>
          )}
          {sharedTree && (
            <p class="board-run-warn" role="alert">
              {concurrency} agents will work in the same checkout at once and can
              overwrite each other's files and commits. Turn on{' '}
              <strong>Isolated worktree per item</strong>, or set At once to 1.
            </p>
          )}
          {workdir && isolate && (
            <p class="board-cred-note">
              Each item works on its own <span class="mono">kc/…</span> branch in its
              own folder. The Review card shows what it changed; a sent-back item
              continues in the same worktree.
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
                {run.worktree_clamp_reason && (
                  <p class="board-run-clamp">{run.worktree_clamp_reason}</p>
                )}
                {run.warnings?.includes('shared_tree') && (
                  <p class="board-run-clamp">
                    These agents shared one checkout ({run.workdir}) — their edits may
                    overlap.
                  </p>
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
      {run.isolate && (
        <span class="board-run-isolated" title={`Each item in its own worktree of ${run.workdir}`}>
          isolated
        </span>
      )}
      {run.warnings?.includes('shared_tree') && (
        <span class="board-run-partial" title={`Several agents shared ${run.workdir}`}>
          shared tree
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
 *
 * There is deliberately no elapsed time (#704). The only timestamp a row has is
 * `updated_at`, which the server rewrites on every change to the row, so a
 * clock built on it measured "since this row last changed" rather than "time
 * spent on this ticket" — a number that looked like progress and was not.
 */
function RunItemTable({ run }: { run: BoardRun }) {
  const items = Object.values(run.items ?? {});

  // Items that have a review card on THIS board — the only outcomes that can
  // open one. A build that failed before reporting has none, and a queue read
  // for another board must not make this board's rows look clickable.
  const reviewable = new Set(
    reviewBoardId.value === run.board_id
      ? reviewGroups.value.flatMap((g) => g.items).map((r) => String(r.item_id))
      : [],
  );

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
          <th scope="col">Session</th>
        </tr>
      </thead>
      <tbody>
        {sorted.map((item) => {
          const live = isRunItemLive(item.state);
          return (
            <tr key={item.id} class={`board-run-item-${item.state}`}>
              <td class="mono">{item.key || item.id}</td>
              <td class="board-run-item-title">
                {item.title}
                {item.worktree?.branch && (
                  <span class="board-run-item-branch mono" title={item.worktree.path}>
                    ⎇ {item.worktree.branch}
                  </span>
                )}
              </td>
              <td>
                <span class={`board-state-pill board-state-${item.state}`}>
                  {item.state === 'working' && (
                    <span class="board-spinner" aria-hidden="true" />
                  )}
                  {runItemStateLabel(item.state)}
                </span>
              </td>
              <td>
                {/* A `failed` disposition next to a FAILED state pill says the
                    same word twice; the error underneath is the part that
                    carries information. */}
                {item.disposition &&
                  item.disposition !== item.state &&
                  (reviewable.has(String(item.id)) ? (
                    // The outcome is where a reviewer's next step starts, so
                    // it opens that ticket's card rather than leaving them to
                    // find it in the queue (#704). It appears as soon as the
                    // review read that follows the agent's report lands.
                    <button
                      type="button"
                      class={`board-disposition board-disposition-${item.disposition} board-outcome-link`}
                      aria-label={`Open the review for ${item.key || item.id}: ${dispositionLabel(item.disposition)}`}
                      onClick={() => openReviewFor(item.id)}
                    >
                      {dispositionLabel(item.disposition)}
                      <span aria-hidden="true">→</span>
                    </button>
                  ) : (
                    <span
                      class={`board-disposition board-disposition-${item.disposition}`}
                    >
                      {dispositionLabel(item.disposition)}
                    </span>
                  ))}
                {item.error && (
                  <span class="board-run-item-error">{item.error}</span>
                )}
                {!item.disposition && !item.error && live && (
                  <span class="board-run-item-waiting">—</span>
                )}
              </td>
              <td class="board-run-item-session">
                <SessionLink item={item} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/**
 * The way from a run item to the agent that worked it (#704).
 *
 * Every dispatched item is a Build, and its page is where the agent's live
 * terminal and its history are. The table used to stop at the agent's
 * conclusion, so "where is it working on this?" had no answer on screen.
 *
 * A real link, so it can be opened in a new tab; an ordinary click stays in
 * the dashboard, and coming back returns to this tab (the tab lives in the
 * store). An item with no Build yet — queued, or refused before it started —
 * has nothing to link to and says so with a dash.
 */
function SessionLink({ item }: { item: BoardRunItem }) {
  if (!item.task_id) {
    return <span class="board-run-item-waiting">—</span>;
  }
  const path = `/tasks/${encodeURIComponent(item.task_id)}`;
  return (
    <a
      class="board-run-session-link"
      href={routeHref(path)}
      aria-label={`View the session for ${item.key || item.id}`}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) {
          return;
        }
        e.preventDefault();
        navigate(path);
      }}
    >
      View session
    </a>
  );
}
