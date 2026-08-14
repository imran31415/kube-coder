import { useEffect, useState } from 'preact/hooks';
import {
  boardsError,
  openReviewCount,
  refreshBoards,
  refreshReview,
  reviewFocusItemId,
  selectBoard,
  selectedItemId,
  startBoardsEvents,
  stopBoardsEvents,
} from '../../store/boards';
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

type Tab = 'items' | 'runs' | 'review' | 'credentials';

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
              </button>
            ))}
          </nav>
        </header>
        {boardsError.value && <p class="board-error">{boardsError.value}</p>}

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

