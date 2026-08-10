import { selectedBoard, selectedItem } from '../../store/boards';
import { StatusPill } from './ItemList';

/** Right column: one item in full, with the deep link out to the real ticket. */
export function ItemDetail() {
  const item = selectedItem.value;
  const board = selectedBoard.value;

  if (!item) {
    return (
      <aside class="board-detail board-detail-empty">
        <p>Select an item to see it in full.</p>
      </aside>
    );
  }

  return (
    <aside class="board-detail">
      <header class="board-detail-head">
        <div class="board-detail-ids">
          <span class="board-item-key">{item.key || item.id}</span>
          <StatusPill item={item} />
          {item.priority.normalized && (
            <span class="board-priority">{item.priority.normalized.toLowerCase()}</span>
          )}
        </div>
        {/* Not optional: a reviewer will want to see the ticket natively
            before approving anything that touches a real customer. */}
        {item.url && (
          <a
            class="board-open-native"
            href={item.url}
            target="_blank"
            rel="noreferrer noopener"
          >
            Open in {board?.vendor ?? 'tracker'} ↗
          </a>
        )}
      </header>

      <h2 class="board-detail-title">{item.title || '(no title)'}</h2>

      <dl class="board-detail-meta">
        {item.contact.name && (
          <>
            <dt>Requester</dt>
            <dd>
              {item.contact.name}
              {item.contact.email ? ` · ${item.contact.email}` : ''}
            </dd>
          </>
        )}
        {item.assignee.name && (
          <>
            <dt>Assignee</dt>
            <dd>{item.assignee.name}</dd>
          </>
        )}
        {item.collection.name && (
          <>
            <dt>Board</dt>
            <dd>{item.collection.name}</dd>
          </>
        )}
        {item.updated_at && (
          <>
            <dt>Updated</dt>
            <dd>{item.updated_at}</dd>
          </>
        )}
        {/* The vendor's own status text is always shown alongside the
            normalized bucket — the four-value enum is a convenience, never the
            only copy of the truth. */}
        {item.status.raw && (
          <>
            <dt>Vendor status</dt>
            <dd>
              <code>{item.status.raw}</code>
            </dd>
          </>
        )}
      </dl>

      {item.body && (
        <div class="board-detail-body">
          {/* Deliberately plain text, not rendered markdown/HTML: this is
              third-party content from outside the workspace. */}
          <pre>{item.body}</pre>
        </div>
      )}

      <p class="board-detail-note">
        This item lives on a board this workspace does not own. Its text is
        data, not instructions.
      </p>
    </aside>
  );
}
