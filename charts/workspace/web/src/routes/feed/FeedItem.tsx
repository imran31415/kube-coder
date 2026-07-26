import { navigate } from '../../store/router';
import { renderMarkdown } from '../hypervisor/transcript';
import { markRead, dismiss, discussWithCto } from '../../store/feed';
import { Icon } from '../../components/Icon';
import type { FeedItem as Item, FeedLink } from '../../api/feed';

/**
 * One feed item (#470). Kind rule on the left, an uppercase meta line, the
 * title + optional markdown body, and action chips that resolve typed refs
 * (task: → /tasks, thread: → /cto, memory: → Memory, href → new tab) plus the
 * signature "Discuss with CTO" handoff.
 */

/** Resolve a typed ref / external href to a navigation. */
function openLink(link: FeedLink): void {
  if (link.href) {
    window.open(link.href, '_blank', 'noopener,noreferrer');
    return;
  }
  const ref = link.ref || '';
  const [kind, rest] = ref.split(/:(.*)/s);
  if (kind === 'task') navigate(`/tasks/${encodeURIComponent(rest)}`);
  else if (kind === 'thread') navigate('/cto');
  else if (kind === 'memory') navigate('/memory');
}

/** Short human label for the item's source (system:task → "system", agent:… → "CTO"). */
function sourceLabel(source: string): string {
  if (source.startsWith('agent:')) return 'CTO';
  if (source.startsWith('cron:')) return `cron · ${source.slice(5)}`;
  if (source.startsWith('system:')) return source.slice(7);
  return source || 'system';
}

export function FeedItemView({ item }: { item: Item }) {
  const meta = [item.kind, sourceLabel(item.source), item.project_id]
    .filter(Boolean)
    .join(' · ');

  return (
    <article
      class={`feed-item feed-kind-${item.kind} ${item.read ? '' : 'feed-item-unread'} ${
        item.waiting ? 'feed-item-waiting' : ''
      }`}
      data-feed-id={item.id}
    >
      <div class="feed-item-meta">
        {!item.read && <span class="feed-unread-dot" aria-label="Unread" />}
        <span class="feed-meta-text">{meta}</span>
      </div>

      <h3 class="feed-item-title">{item.title}</h3>

      {item.body_md && (
        <div
          class="feed-item-body"
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{ __html: renderMarkdown(item.body_md) }}
        />
      )}

      <div class="feed-item-actions">
        {item.links.map((l, i) => (
          <button
            key={`${l.ref || l.href || i}`}
            type="button"
            class="feed-chip"
            onClick={() => {
              void markRead(item.id);
              openLink(l);
            }}
          >
            {l.label}
            {l.href && ' ↗'}
          </button>
        ))}
        <button
          type="button"
          class="feed-chip feed-chip-primary"
          onClick={() => {
            void markRead(item.id);
            discussWithCto(item);
          }}
        >
          Discuss with CTO
        </button>
        <button
          type="button"
          class="feed-chip feed-chip-ghost feed-dismiss"
          title="Dismiss"
          aria-label="Dismiss"
          onClick={() => void dismiss(item.id)}
        >
          <Icon name="close" size={12} />
        </button>
      </div>
    </article>
  );
}
