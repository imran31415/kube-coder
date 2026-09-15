import { useEffect, useState } from 'preact/hooks';
import {
  boardsError,
  boardsLive,
  boardStanding,
  lastRunSync,
  openReviewCount,
  refreshBoards,
  refreshReview,
  reviewFocusItemId,
  selectBoard,
  selectedBoardId,
  selectedItemId,
  startBoardsEvents,
  startRunPolling,
  stopBoardsEvents,
  stopRunPolling,
  type BoardTab,
} from '../../store/boards';
import { elapsedLabel } from '../../api/boards';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import { BoardRail } from './BoardRail';
import { ItemList } from './ItemList';
import { ItemDetail } from './ItemDetail';
import { ReviewPanel } from './ReviewPanel';
import { RunsPanel } from './RunsPanel';
import { CredentialsPanel } from './CredentialsPanel';
import { ConnectBoard } from './ConnectBoard';
import './board.css';

const RAIL_COLLAPSED_KEY = 'kc.boardRailCollapsed';

/** The store names the same four tabs, because `boardStanding` has to be able
 *  to point at one. Aliasing rather than restating it keeps them from
 *  drifting apart. */
type Tab = BoardTab;

const TABS: { id: Tab; label: string }[] = [
  { id: 'items', label: 'Items' },
  { id: 'runs', label: 'Runs' },
  { id: 'review', label: 'Review' },
  { id: 'credentials', label: 'Credentials' },
];

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(RAIL_COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

/**
 * The Board Processor surface (#588/#589).
 *
 * Four tabs, in the order the work actually happens: read the board, run it,
 * review what the agents propose, and manage the credentials that let any of
 * it authenticate.
 *
 * Layout mirrors /cto: rail | main | detail, stacking below 860px so the same
 * breakpoint governs CSS and JS.
 */
export function BoardRoute() {
  const narrow = useMediaQuery('(max-width: 860px)');
  const [railCollapsed, setRailCollapsed] = useState(readCollapsed);
  const [tab, setTab] = useState<Tab>('items');
  const [connecting, setConnecting] = useState(false);

  useEffect(() => {
    void refreshBoards();
    startBoardsEvents();

    // A feed link or the waiting badge deep-links straight to one item's
    // review card: /board?board=<id>&review=<item_id>. Arriving anywhere else
    // in the queue would make "N waiting" a scavenger hunt.
    const params = new URLSearchParams(window.location.search);
    const boardId = params.get('board');
    const reviewItem = params.get('review');
    if (boardId) {
      void selectBoard(boardId).then(() => {
        if (reviewItem !== null) void refreshReview(boardId);
      });
      if (reviewItem !== null) {
        reviewFocusItemId.value = reviewItem;
        setTab('review');
      }
    }
    return () => stopBoardsEvents();
  }, []);

  // Run progress is polled by the ROUTE, not by RunsPanel, because the route
  // survives tab switches and the panel does not. Watching a run means
  // bouncing to Review to act on what it produced, and owning the poll in the
  // panel meant that trip stopped the very updates it was meant to deliver.
  const boardId = selectedBoardId.value;
  useEffect(() => {
    if (!boardId) return;
    startRunPolling(boardId);
    return () => stopRunPolling();
  }, [boardId]);

  function toggleRail() {
    setRailCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(RAIL_COLLAPSED_KEY, next ? '1' : '0');
      } catch {
        /* private mode — the preference just doesn't persist */
      }
      return next;
    });
  }

  const pending = openReviewCount.value;
  const standing = boardStanding.value;

  // The detail column only earns its width once something is in it. With no
  // item selected — and on every tab other than Items, which has no detail
  // column at all — it collapses so the list gets the space back instead of
  // reserving 340px of "Select an item to see it in full."
  const detailOpen = tab === 'items' && !!selectedItemId.value;

  return (
    <div
      class={[
        'route route-board',
        railCollapsed ? 'board-rail-is-collapsed' : '',
        detailOpen ? '' : 'board-detail-is-collapsed',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {!narrow && (
        <BoardRail
          collapsed={railCollapsed}
          onToggleCollapse={toggleRail}
          onConnect={() => setConnecting(true)}
        />
      )}
      {narrow && <BoardRail onConnect={() => setConnecting(true)} />}

      <section class="board-main">
        <header class="board-topbar">
          <h1>Board</h1>
          <p class="board-subtitle">
            Items from a tracker this workspace does not own.
          </p>
          <nav class="board-tabs" aria-label="Board sections">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                class={`board-tab ${tab === t.id ? 'is-active' : ''}`}
                aria-current={tab === t.id ? 'page' : undefined}
                onClick={() => setTab(t.id)}
              >
                {t.label}
                {t.id === 'review' && pending > 0 && (
                  <span class="board-tab-badge mono">{pending}</span>
                )}
                {/* A run in flight is the one thing that changes while you are
                    looking at another tab, so it is the one thing the tab strip
                    has to be able to say from anywhere. */}
                {t.id === 'runs' && standing.live && (
                  <span
                    class="board-tab-live"
                    title="A run is in flight"
                    aria-label="A run is in flight"
                  />
                )}
              </button>
            ))}
          </nav>
        </header>
        {boardsError.value && <p class="board-error">{boardsError.value}</p>}

        <StandingStrip tab={tab} onGo={setTab} />

        {tab === 'items' && <ItemList />}
        {tab === 'runs' && <RunsPanel />}
        {tab === 'review' && <ReviewPanel />}
        {tab === 'credentials' && <CredentialsPanel />}
      </section>

      {tab === 'items' && <ItemDetail />}

      <ConnectBoard open={connecting} onClose={() => setConnecting(false)} />
    </div>
  );
}

const GO_LABEL: Record<Tab, string> = {
  items: 'Open Items',
  runs: 'Start a run',
  review: 'Open Review',
  credentials: 'Add a credential',
};

/**
 * Where the board stands, and the one thing to do next.
 *
 * The four tabs each answer a different question, and none of them answered
 * the first question an operator actually has — *what is happening, and what
 * should I do now*. Working that out meant visiting Items to count, Runs to
 * see whether anything was moving, and Review to see whether anything was
 * waiting, which is what the maintainer meant by "hard to tell the state of
 * the board and items and next steps easily".
 *
 * It also carries the freshness of everything above it. A dropped event stream
 * used to be indistinguishable from a quiet board: the numbers simply stopped,
 * and nothing on the page admitted why.
 */
function StandingStrip({
  tab,
  onGo,
}: {
  tab: Tab;
  onGo: (t: Tab) => void;
}) {
  const standing = boardStanding.value;
  const live = boardsLive.value;
  const synced = lastRunSync.value;
  // Re-render on a slow tick so "updated 40s ago" does not freeze at the
  // moment of the last fetch. Only needed while the stream is down.
  const [, setTick] = useState(0);

  useEffect(() => {
    if (live) return;
    const t = setInterval(() => setTick((n) => n + 1), 5000);
    return () => clearInterval(t);
  }, [live]);

  if (!standing.next) return null;

  // The button only appears where it would actually take you somewhere —
  // telling a reader already on the review queue to "open Review" is noise
  // dressed as guidance.
  const go = standing.nextTab && standing.nextTab !== tab ? standing.nextTab : null;

  return (
    <div class="board-standing" role="status">
      <p class="board-standing-next">
        {standing.next}
        {go && (
          <button
            type="button"
            class="btn btn-ghost btn-sm board-standing-go"
            onClick={() => onGo(go)}
          >
            {GO_LABEL[go]}
          </button>
        )}
      </p>
      <div class="board-standing-facts">
        {standing.items > 0 && (
          <span class="board-standing-fact">
            <span class="mono">{standing.items}</span> items
          </span>
        )}
        {standing.live && (
          <span class="board-standing-fact">
            <span class="mono">
              {standing.settled}/{standing.runTotal}
            </span>{' '}
            worked
          </span>
        )}
        {standing.working > 0 && (
          <span class="board-standing-fact board-standing-working">
            <span class="board-spinner" aria-hidden="true" />
            <span class="mono">{standing.working}</span> working
          </span>
        )}
        {standing.queued > 0 && (
          <span class="board-standing-fact">
            <span class="mono">{standing.queued}</span> queued
          </span>
        )}
        {standing.awaiting > 0 && (
          <span class="board-standing-fact board-standing-awaiting">
            <span class="mono">{standing.awaiting}</span> awaiting you
          </span>
        )}
        {/* Never "live" on faith: this reads the stream's own connection
            state, so a page that has quietly stopped receiving updates says
            so rather than showing stale numbers with a confident label. */}
        <span
          class={`board-standing-live ${live ? 'is-live' : 'is-stale'}`}
          title={
            live
              ? 'Updates are streaming from the workspace'
              : 'The update stream is down — falling back to polling'
          }
        >
          {live
            ? 'live'
            : synced
              ? `updated ${elapsedLabel(Math.floor(synced / 1000))} ago`
              : 'not live'}
        </span>
      </div>
    </div>
  );
}
