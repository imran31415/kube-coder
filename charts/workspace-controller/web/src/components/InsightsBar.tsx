import { useEffect, useState } from 'preact/hooks';
import { type Advisory, getInsights } from '../api/workspaces';
import { navigate } from '../router';

const ICON: Record<string, string> = { critical: '⛔', warn: '⚠', info: 'ℹ' };

/** Automatic, data-driven tips shown atop the dashboard. Polls slowly (60s) —
 *  insights are derived from hours of history, so they don't change fast. */
export function InsightsBar() {
  const [adv, setAdv] = useState<Advisory[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const r = await getInsights();
        if (alive) {
          setAdv(r.advisories);
          setErr(r.error);
        }
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoaded(true);
      }
    }
    void load();
    const id = window.setInterval(() => void load(), 60000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  if (!loaded) return null;
  if (adv.length === 0) {
    return (
      <div class="insights">
        <div class="insight calm">
          {err ? `Insights unavailable: ${err}` : '✓ All workspaces look healthy.'}
        </div>
      </div>
    );
  }
  return (
    <div class="insights" aria-label="Insights">
      {adv.map((a, i) => (
        <button
          key={`${a.user}-${a.kind}-${i}`}
          class={`insight sev-${a.severity}`}
          onClick={() => navigate(`/w/${a.user}`)}
          title={`Open ${a.user}`}
        >
          <span class="insight-icon" aria-hidden="true">{ICON[a.severity] ?? 'ℹ'}</span>
          <span class="insight-msg">{a.message}</span>
        </button>
      ))}
    </div>
  );
}
