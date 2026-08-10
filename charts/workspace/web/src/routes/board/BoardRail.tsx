import { boards, boardsLoading, selectedBoardId, selectBoard } from '../../store/boards';
import { Icon } from '../../components/Icon';
import { MutatorOnly } from '../../components/MutatorOnly';

/** Left rail: one card per connected board. Mirrors ProjectRail's shape (a
 *  dumb-ish nav that reads the store directly) so /board and /cto feel like
 *  the same surface. */
export function BoardRail({
  collapsed = false,
  onToggleCollapse,
  onConnect,
}: {
  collapsed?: boolean;
  onToggleCollapse?: () => void;
  /** Opens the connect dialog. Owned by the route because the rail renders
   *  twice (narrow and wide) and two dialogs would be one too many. */
  onConnect?: () => void;
}) {
  const list = boards.value;

  if (collapsed) {
    return (
      <nav class="board-rail board-rail-collapsed" aria-label="Boards">
        <button
          type="button"
          class="board-rail-toggle"
          onClick={onToggleCollapse}
          aria-label="Expand boards rail"
        >
          <Icon name="chevron-right" />
        </button>
        {list.map((b) => (
          <button
            key={b.id}
            type="button"
            class={`board-rail-chip ${selectedBoardId.value === b.id ? 'is-active' : ''}`}
            title={b.display_name}
            onClick={() => void selectBoard(b.id)}
          >
            {initials(b.display_name)}
          </button>
        ))}
      </nav>
    );
  }

  return (
    <nav class="board-rail" aria-label="Boards">
      <header class="board-rail-head">
        <h2>Boards</h2>
        {onToggleCollapse && (
          <button
            type="button"
            class="board-rail-toggle"
            onClick={onToggleCollapse}
            aria-label="Collapse boards rail"
          >
            <Icon name="chevron-left" />
          </button>
        )}
      </header>

      {boardsLoading.value && list.length === 0 && (
        <p class="board-rail-empty">Loading boards…</p>
      )}

      {!boardsLoading.value && list.length === 0 && (
        <div class="board-rail-empty">
          <p>No boards connected.</p>
          <p class="board-rail-hint">
            A board is an external tracker — Jira, GitHub, Linear, Zendesk —
            reached through a connector. Start from a template for GitHub
            Issues, Jira Cloud or Zendesk; for anything else, ask an agent to
            author the connector rather than hand-writing the JSON.
          </p>
        </div>
      )}

      <ul class="board-rail-list">
        {list.map((b) => (
          <li key={b.id}>
            <button
              type="button"
              class={`board-card ${selectedBoardId.value === b.id ? 'is-active' : ''}`}
              onClick={() => void selectBoard(b.id)}
            >
              <span class="board-card-name">{b.display_name}</span>
              <span class="board-card-meta">
                <span class="board-vendor">{b.vendor}</span>
                {/* A board whose credential no longer resolves would fail every
                    fetch; say so on the card rather than in an error toast. */}
                {!b.credential_set && b.credential_ref && (
                  <span class="board-needs-key" title={b.credential_ref}>
                    needs key
                  </span>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>

      {onConnect && (
        <MutatorOnly>
          <button
            type="button"
            class="btn btn-ghost btn-sm board-rail-connect"
            onClick={onConnect}
          >
            + Connect a board
          </button>
        </MutatorOnly>
      )}
    </nav>
  );
}

function initials(name: string): string {
  return name
    .split(/[\s—-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? '')
    .join('');
}
