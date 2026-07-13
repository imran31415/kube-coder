import { useEffect, useState } from 'preact/hooks';
import { listTasks, isStaleWaiting, type TaskSummary } from '../../api/tasks';
import { listThreads, type HypervisorThread, type ThreadStatus } from '../../api/hypervisor';
import { navigate } from '../../store/router';
import { Icon } from '../../components/Icon';
import { DesktopSection } from './DesktopSection';

interface BuildEntry {
  id: string;            // task_id
  title: string;
  snippet: string;
  ts: number;            // unix seconds
  status: 'running' | 'waiting-for-input';
  stale: boolean;        // waiting + idle past the stale threshold
}

interface ChatEntry {
  id: string;            // thread id
  title: string;
  ts: number;            // unix seconds (updated_at)
  status: ThreadStatus;
}

// Sort key: stale-waiting first, then waiting, then running — so the
// sessions that need the user's input are always surfaced before busy ones.
function attentionRank(e: BuildEntry): number {
  if (e.stale) return 0;
  if (e.status === 'waiting-for-input') return 1;
  return 2;
}

const STATUS_LABEL: Record<BuildEntry['status'], string> = {
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

export function DesktopBulletin() {
  const [builds, setBuilds] = useState<BuildEntry[]>([]);
  const [chats, setChats] = useState<ChatEntry[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      // Builds and chats are independent, best-effort fetches — a failure of
      // one must never blank the other, so each keeps its last-good list.
      try {
        const tasks = await listTasks();
        if (!cancelled) {
          const live: BuildEntry[] = (tasks ?? [])
            .filter((t: TaskSummary) =>
              t.status === 'running' || t.status === 'waiting-for-input',
            )
            .map((t: TaskSummary) => ({
              id: t.task_id,
              title: t.name || t.task_id.slice(0, 16),
              snippet: (t.prompt || '').slice(0, 140),
              ts: t.created_at ?? 0,
              status: t.status as BuildEntry['status'],
              stale: isStaleWaiting(t),
            }))
            // Waiting/stale first (needs input), then most-recent within group.
            .sort((a, b) => attentionRank(a) - attentionRank(b) || b.ts - a.ts)
            .slice(0, 3);
          setBuilds(live);
        }
      } catch {
        /* keep last-good builds */
      }
      try {
        const threads = await listThreads();
        if (!cancelled) {
          // listThreads() already returns newest-first (updated_at desc);
          // take the 3 most-recent chats.
          const recent: ChatEntry[] = (threads ?? [])
            .slice(0, 3)
            .map((t: HypervisorThread) => ({
              id: t.id,
              title: t.title || 'New chat',
              ts: t.updated_at ?? t.created_at ?? 0,
              status: t.status,
            }));
          setChats(recent);
        }
      } catch {
        /* keep last-good chats */
      }
      if (!cancelled) setLoaded(true);
    }
    void refresh();
    // Poll every 10s so the widget reflects newly-started / completed builds
    // and chats without a page refresh. Same cadence as the rest of the
    // dashboard's task list refresh.
    const id = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void refresh();
    }, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Hide the whole widget only when there's nothing to show in EITHER group —
  // the desktop stays clean instead of carrying an empty card.
  if (loaded && builds.length === 0 && chats.length === 0) return null;

  return (
    <DesktopSection
      class="dt-section-activity"
      title="Activity"
      icon={<Icon name="tasks" size={13} />}
      meta={<span class="dt-bulletin-hint muted">Click to jump in</span>}
      data-dt-stop="true"
      aria-label="Live builds and recent chats"
    >
      <div class="dt-bulletin">
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
          <>
            {builds.length > 0 && (
              <>
                <p class="dt-bulletin-group muted">Live builds</p>
                <ul class="dt-bulletin-list" role="list">
                  {builds.map((e) => (
                    <li key={e.id}>
                      <button
                        type="button"
                        class="dt-bulletin-row"
                        onClick={() => navigate(`/tasks/${encodeURIComponent(e.id)}`)}
                        title={e.snippet || e.title}
                      >
                        <span class={`dt-bulletin-tag dt-bulletin-tag-${e.stale ? 'stale' : e.status === 'waiting-for-input' ? 'waiting' : 'running'}`}>
                          {e.status === 'running' && (
                            <span class="dt-bulletin-running" aria-hidden="true" />
                          )}
                          {e.stale ? 'Needs you' : STATUS_LABEL[e.status]}
                        </span>
                        <span class="dt-bulletin-row-body">
                          <span class="dt-bulletin-title-text">{e.title}</span>
                          {e.snippet && (
                            <span class="dt-bulletin-snippet muted">{e.snippet}</span>
                          )}
                        </span>
                        <span class="dt-bulletin-time muted mono">{relativeTime(e.ts)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}

            {chats.length > 0 && (
              <>
                <p class="dt-bulletin-group muted">Recent chats</p>
                <ul class="dt-bulletin-list" role="list">
                  {chats.map((c) => (
                    <li key={c.id}>
                      <button
                        type="button"
                        class="dt-bulletin-row"
                        onClick={() => navigate(`/hypervisor/${encodeURIComponent(c.id)}`)}
                        title={c.title}
                      >
                        <span class={`dt-bulletin-tag dt-bulletin-tag-chat dt-bulletin-tag-chat-${c.status}`}>
                          {c.status === 'running' && (
                            <span class="dt-bulletin-running" aria-hidden="true" />
                          )}
                          <Icon name="chat" size={11} />
                          Chat
                        </span>
                        <span class="dt-bulletin-row-body">
                          <span class="dt-bulletin-title-text">{c.title}</span>
                        </span>
                        <span class="dt-bulletin-time muted mono">{relativeTime(c.ts)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </div>
    </DesktopSection>
  );
}
