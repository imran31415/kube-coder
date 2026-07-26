import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { EmptyState } from '../../components/primitives/EmptyState';
import { serverMode } from '../../store/server-mode';
import {
  filteredFeed,
  feedKindFilter,
  feedProjectFilter,
  feedLoading,
  setKindFilter,
  markRead,
  groupByDay,
  startFeedPolling,
  stopFeedPolling,
  type FeedFilter,
} from '../../store/feed';
import { projects } from '../../store/projects';
import { FeedItemView } from './FeedItem';
import './feed.css';

const KIND_CHIPS: { value: FeedFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'briefing', label: 'Briefings' },
  { value: 'news', label: 'News' },
  { value: 'activity', label: 'Activity' },
  { value: 'decision', label: 'Decisions' },
];

export function FeedRoute() {
  const disabled = serverMode.value.ctoEnabled === false;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // ts of the newest item the reader has seen at the top; new items above it
  // wait behind a "N new" pill rather than reflow-jumping under them.
  const seenTopTs = useRef<number>(0);
  const [newCount, setNewCount] = useState(0);

  useEffect(() => {
    if (disabled) return;
    startFeedPolling();
    return () => stopFeedPolling();
  }, [disabled]);

  const items = filteredFeed.value;
  const groups = useMemo(() => groupByDay(items), [items]);

  // Mark items read as they enter the viewport (debounced by markRead's own
  // already-read guard). Re-observe whenever the rendered set changes.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root || typeof IntersectionObserver === 'undefined') return;
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting && e.intersectionRatio > 0.4) {
            const id = (e.target as HTMLElement).dataset.feedId;
            if (id) void markRead(id);
          }
        }
      },
      { root, threshold: 0.4 },
    );
    root.querySelectorAll('.feed-item-unread[data-feed-id]').forEach((el) => obs.observe(el));
    return () => obs.disconnect();
  }, [items]);

  // "N new" pill: while the reader is scrolled down, count items newer than the
  // last top they saw. At (or near) the top, adopt the newest ts and clear it.
  useEffect(() => {
    const newest = items[0]?.ts ?? 0;
    const root = scrollRef.current;
    const atTop = !root || root.scrollTop < 40;
    if (atTop) {
      seenTopTs.current = newest;
      setNewCount(0);
    } else {
      setNewCount(items.filter((i) => i.ts > seenTopTs.current).length);
    }
  }, [items]);

  function onScroll() {
    const root = scrollRef.current;
    if (root && root.scrollTop < 40) {
      seenTopTs.current = items[0]?.ts ?? 0;
      setNewCount(0);
    }
  }

  function jumpToTop() {
    scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
    seenTopTs.current = items[0]?.ts ?? 0;
    setNewCount(0);
  }

  if (disabled) {
    return (
      <div class="route route-feed route-feed-disabled">
        <EmptyState
          icon={<Icon name="feed" size={26} />}
          title="Feed is disabled"
          description={
            <>
              The Feed rides the AI CTO — enable <code>cto.enabled</code> in the
              workspace chart.
            </>
          }
        />
      </div>
    );
  }

  const activeProjects = projects.value.filter((p) => p.status !== 'archived');

  return (
    <div class="route route-feed">
      <div class="feed-shell">
        <header class="feed-head">
          <div class="feed-title-row">
            <h1 class="feed-masthead">Feed</h1>
            <span class="feed-sub">What changed · what matters</span>
          </div>
          <div class="feed-filters" role="tablist" aria-label="Filter feed">
            {KIND_CHIPS.map((c) => (
              <button
                key={c.value}
                type="button"
                role="tab"
                aria-selected={feedKindFilter.value === c.value}
                class={`feed-filter ${feedKindFilter.value === c.value ? 'feed-filter-on' : ''}`}
                onClick={() => setKindFilter(c.value)}
              >
                {c.label}
              </button>
            ))}
            {activeProjects.length > 0 && (
              <select
                class="feed-project-select"
                aria-label="Filter by project"
                value={feedProjectFilter.value ?? ''}
                onChange={(e) =>
                  (feedProjectFilter.value = (e.target as HTMLSelectElement).value || null)
                }
              >
                <option value="">All projects</option>
                {activeProjects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            )}
          </div>
        </header>

        {newCount > 0 && (
          <button type="button" class="feed-new-pill" onClick={jumpToTop}>
            <Icon name="chevron-up" size={12} /> {newCount} new
          </button>
        )}

        <div class="feed-stream" ref={scrollRef} onScroll={onScroll}>
          {items.length === 0 ? (
            <p class="feed-empty">
              {feedLoading.value
                ? 'Loading…'
                : 'Nothing yet — your CTO and workspace will post here as things happen.'}
            </p>
          ) : (
            groups.map((g) => (
              <section key={g.key} class="feed-day">
                <h2 class="feed-day-label">{g.label}</h2>
                {g.items.map((it) => (
                  <FeedItemView key={it.id} item={it} />
                ))}
              </section>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
