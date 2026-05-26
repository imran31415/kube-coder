import { useEffect, useRef, useState } from 'preact/hooks';
import { type Advisory, getInsights } from '../api/workspaces';
import { navigate } from '../router';

const ICON: Record<string, string> = { critical: '⛔', warn: '⚠', info: 'ℹ' };

/** Automatic, data-driven tips shown atop the dashboard. Polls slowly (60s) —
 *  insights are derived from hours of history, so they don't change fast.
 *
 *  The first request can transiently fail (the controller's kubectl discovery
 *  cache is cold right after a pod roll, and several endpoints fire at once on
 *  page load). Rather than flash "Insights unavailable", we show a calm loading
 *  state and retry a few times quietly, only surfacing an error if it persists.
 *  Once we have data, a later poll failure is ignored so the bar never flickers. */
export function InsightsBar() {
  const [adv, setAdv] = useState<Advisory[] | null>(null); // null = never loaded
  const [err, setErr] = useState<string | null>(null);
  const [showError, setShowError] = useState(false);
  const hasData = useRef(false);
  const attempts = useRef(0);

  useEffect(() => {
    let alive = true;
    let retry: number | undefined;
    async function load() {
      try {
        const r = await getInsights();
        if (!alive) return;
        hasData.current = true;
        attempts.current = 0;
        setAdv(r.advisories);
        setErr(r.error);
        setShowError(false);
      } catch (e) {
        if (!alive) return;
        attempts.current += 1;
        if (!hasData.current && attempts.current < 4) {
          retry = window.setTimeout(() => void load(), 2000); // transient — retry quietly
        } else if (!hasData.current) {
          setErr(e instanceof Error ? e.message : String(e));
          setShowError(true);
        }
        // If we already have data, ignore a transient poll failure.
      }
    }
    void load();
    const poll = window.setInterval(() => void load(), 60000);
    return () => {
      alive = false;
      window.clearInterval(poll);
      if (retry) window.clearTimeout(retry);
    };
  }, []);

  if (adv === null && !showError) {
    return (
      <div class="insights">
        <div class="insight calm loading">Analyzing workspace usage…</div>
      </div>
    );
  }
  if (showError) {
    return (
      <div class="insights">
        <div class="insight calm">Insights unavailable: {err}</div>
      </div>
    );
  }
  if (adv!.length === 0) {
    return (
      <div class="insights">
        <div class="insight calm">
          {err ? `Live metrics unavailable: ${err}` : '✓ All workspaces look healthy.'}
        </div>
      </div>
    );
  }
  return (
    <div class="insights" aria-label="Insights">
      {adv!.map((a, i) => (
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
