import { useEffect, useState } from 'preact/hooks';
import { listTasks, type TaskSummary } from '../../api/tasks';
import { navigate } from '../../store/router';

interface BulletinEntry {
  kind: 'build';
  id: string;            // task_id
  title: string;
  snippet: string;
  ts: number;            // unix seconds
  status: 'running' | 'waiting-for-input';
}

const STATUS_LABEL: Record<BulletinEntry['status'], string> = {
  'running': 'Running',
  'waiting-for-input': 'Waiting',
};

function relativeTime(ts: number): string {
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d`;
  return new Date(ts * 1000).toLocaleDateString();
}

function navigateToEntry(entry: BulletinEntry) {
  navigate(`/tasks/${encodeURIComponent(entry.id)}`);
}

export function DesktopBulletin() {
  const [entries, setEntries] = useState<BulletinEntry[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      try {
        const tasks = await listTasks();
        if (cancelled) return;
        const live: BulletinEntry[] = (tasks ?? [])
          .filter((t: TaskSummary) =>
            t.status === 'running' || t.status === 'waiting-for-input',
          )
          .map((t: TaskSummary) => ({
            kind: 'build' as const,
            id: t.task_id,
            title: t.name || t.task_id.slice(0, 16),
            snippet: (t.prompt || '').slice(0, 140),
            ts: t.created_at ?? 0,
            status: t.status as BulletinEntry['status'],
          }))
          .sort((a: BulletinEntry, b: BulletinEntry) => b.ts - a.ts)
          .slice(0, 3);
        setEntries(live);
      } catch {
        /* keep last-good entries on transient errors */
      } finally {
        if (!cancelled) setLoaded(true);
      }
    }
    void refresh();
    // Poll every 10s so the widget reflects newly-started / completed
    // builds without a page refresh. Same cadence as the rest of the
    // dashboard's task list refresh.
    const id = window.setInterval(refresh, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Hide the widget entirely when there's nothing live — the desktop
  // stays clean instead of carrying an empty card.
  if (loaded && entries.length === 0) return null;

  return (
    <section class="dt-bulletin" data-dt-stop="true" aria-label="Live builds">
      <header class="dt-bulletin-head">
        <span class="dt-bulletin-title">Live builds</span>
        <span class="dt-bulletin-sub muted">Click to jump in</span>
      </header>
      {!loaded ? (
        <ul class="dt-bulletin-list" role="list" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <li key={i}>
              <div class="dt-bulletin-row dt-bulletin-row-skel" aria-hidden="true">
                <span class="dt-skel dt-skel-tag" />
                <span class="dt-skel dt-skel-title" />
                <span class="dt-skel dt-skel-snippet" />
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <ul class="dt-bulletin-list" role="list">
          {entries.map((e) => (
            <li key={e.id}>
              <button
                type="button"
                class="dt-bulletin-row"
                onClick={() => navigateToEntry(e)}
                title={e.snippet || e.title}
              >
                <span class={`dt-bulletin-tag dt-bulletin-tag-${e.status === 'waiting-for-input' ? 'waiting' : 'running'}`}>
                  {e.status === 'running' && (
                    <span class="dt-bulletin-running" aria-hidden="true" />
                  )}
                  {STATUS_LABEL[e.status]}
                </span>
                <span class="dt-bulletin-title-text">{e.title}</span>
                {e.snippet && (
                  <span class="dt-bulletin-snippet muted">{e.snippet}</span>
                )}
                <span class="dt-bulletin-time muted mono">{relativeTime(e.ts)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
