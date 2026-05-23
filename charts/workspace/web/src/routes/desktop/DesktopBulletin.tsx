import { useEffect, useState } from 'preact/hooks';
import { listTasks, type TaskSummary } from '../../api/tasks';
import { listMemories, type MemoryRecord } from '../../api/memory';
import { navigate } from '../../store/router';
import { Icon, type IconName } from '../../components/Icon';

interface BulletinEntry {
  kind: 'build' | 'memory';
  id: string;            // routing handle (task_id or namespace/key)
  title: string;
  snippet: string;
  ts: number;            // unix seconds
  status?: string;       // builds only
}

function relativeTime(ts: number): string {
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d`;
  return new Date(ts * 1000).toLocaleDateString();
}

function tagIcon(kind: BulletinEntry['kind']): IconName {
  return kind === 'build' ? 'tasks' : 'memory';
}

function tagLabel(kind: BulletinEntry['kind']): string {
  return kind === 'build' ? 'Build' : 'Memory';
}

function navigateToEntry(entry: BulletinEntry) {
  if (entry.kind === 'build') navigate(`/tasks/${encodeURIComponent(entry.id)}`);
  else navigate(`/memory?ns=${encodeURIComponent(entry.id.split('/')[0] ?? '')}`);
}

export function DesktopBulletin() {
  const [entries, setEntries] = useState<BulletinEntry[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // Fetch in parallel — neither call needs the other's result.
        // Errors per-stream are swallowed so a failed builds fetch
        // doesn't blank the whole widget.
        const [tasksRes, memRes] = await Promise.allSettled([
          listTasks(),
          listMemories({ limit: 12 }),
        ]);
        const builds: BulletinEntry[] = tasksRes.status === 'fulfilled'
          ? (tasksRes.value ?? []).map((t: TaskSummary) => ({
              kind: 'build' as const,
              id: t.task_id,
              title: t.name || t.task_id.slice(0, 16),
              snippet: (t.prompt || '').slice(0, 140),
              ts: t.created_at ?? 0,
              status: t.status,
            }))
          : [];
        const memories: BulletinEntry[] = memRes.status === 'fulfilled'
          ? (memRes.value.memories ?? []).map((m: MemoryRecord) => ({
              kind: 'memory' as const,
              id: `${m.namespace}/${m.key}`,
              title: `${m.namespace}.${m.key}`,
              snippet: (m.value ?? '').slice(0, 140),
              ts: m.updated_at ?? m.created_at ?? 0,
            }))
          : [];
        if (cancelled) return;
        const merged = [...builds, ...memories]
          .filter((e) => e.ts > 0)
          .sort((a, b) => b.ts - a.ts)
          .slice(0, 4);
        setEntries(merged);
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (loaded && entries.length === 0) return null;

  return (
    <section class="dt-bulletin" data-dt-stop="true" aria-label="Recent activity">
      <header class="dt-bulletin-head">
        <span class="dt-bulletin-title">Recent activity</span>
        <span class="dt-bulletin-sub muted">Latest builds and memories — click to jump</span>
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
            <li key={`${e.kind}:${e.id}`}>
              <button
                type="button"
                class="dt-bulletin-row"
                onClick={() => navigateToEntry(e)}
                title={e.snippet || e.title}
              >
                <span class={`dt-bulletin-tag dt-bulletin-tag-${e.kind}`}>
                  <Icon name={tagIcon(e.kind)} size={11} />
                  {tagLabel(e.kind)}
                </span>
                <span class="dt-bulletin-title-text">
                  {e.status === 'running' && (
                    <span
                      class="dt-bulletin-running"
                      aria-label="Build is running"
                      title="Running"
                    />
                  )}
                  {e.title}
                </span>
                <span class="dt-bulletin-snippet muted">{e.snippet || '—'}</span>
                <span class="dt-bulletin-time muted mono">{relativeTime(e.ts)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
