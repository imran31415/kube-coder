import {
  filteredItems,
  itemFilter,
  itemsError,
  itemsLoading,
  loadItems,
  selectedBoardId,
  selectedItemId,
  selectedItems,
  selectItem,
} from '../../store/boards';
import { truncationLabel, type BoardItem } from '../../api/boards';

/** Middle column: the items on the selected board. */
export function ItemList() {
  const boardId = selectedBoardId.value;
  if (!boardId) {
    return (
      <div class="board-items board-items-empty">
        <p>Select a board to see its items.</p>
      </div>
    );
  }

  const listing = selectedItems.value;
  const loading = itemsLoading.value[boardId];
  const error = itemsError.value[boardId];
  const items = filteredItems.value;

  return (
    <div class="board-items">
      <header class="board-items-head">
        <input
          type="search"
          class="board-filter"
          placeholder="Filter items…"
          value={itemFilter.value}
          onInput={(e) => {
            itemFilter.value = (e.target as HTMLInputElement).value;
          }}
        />
        <button
          type="button"
          class="board-refresh"
          disabled={!!loading}
          onClick={() => void loadItems(boardId)}
        >
          {loading ? 'Reading…' : 'Refresh'}
        </button>
      </header>

      {/* An incomplete listing is a correctness signal, not a cosmetic one:
          acting on a board you have only partly read is how items get missed.
          It is stated plainly rather than tucked into a tooltip. */}
      {listing && !listing.complete && (
        <p class="board-incomplete" role="status">
          <strong>Possibly incomplete.</strong> {truncationLabel(listing)}
        </p>
      )}

      {error && <p class="board-error">{error}</p>}

      {!error && !loading && items.length === 0 && (
        <p class="board-items-empty">
          {itemFilter.value ? 'No items match that filter.' : 'No items.'}
        </p>
      )}

      <ul class="board-item-list">
        {items.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              class={`board-item ${selectedItemId.value === item.id ? 'is-active' : ''}`}
              onClick={() => selectItem(item.id)}
            >
              <span class="board-item-top">
                <span class="board-item-key">{item.key || item.id}</span>
                <StatusPill item={item} />
              </span>
              <span class="board-item-title">{item.title || '(no title)'}</span>
              {item.tags.length > 0 && (
                <span class="board-item-tags">
                  {item.tags.slice(0, 4).map((t) => (
                    <span key={t} class="board-tag">
                      {t}
                    </span>
                  ))}
                </span>
              )}
            </button>
          </li>
        ))}
      </ul>

      {listing && (
        <footer class="board-items-foot">
          {listing.items.length} item{listing.items.length === 1 ? '' : 's'} ·{' '}
          {listing.pages_fetched} page{listing.pages_fetched === 1 ? '' : 's'}
          {listing.complete ? ' · complete' : ' · partial'}
        </footer>
      )}
    </div>
  );
}

/**
 * Shows the NORMALIZED bucket when the connector maps this vendor value, and
 * the vendor's own text when it does not. An unmapped value is not an error —
 * it passes through as raw by design — so it renders as the vendor wrote it
 * rather than as "unknown".
 */
export function StatusPill({ item }: { item: BoardItem }) {
  const { normalized, raw } = item.status;
  if (!normalized && !raw) return null;
  const label = normalized ? normalized.replace('_', ' ').toLowerCase() : raw;
  return (
    <span
      class={`board-status board-status-${(normalized ?? 'raw').toLowerCase()}`}
      title={raw ?? undefined}
    >
      {label}
    </span>
  );
}
