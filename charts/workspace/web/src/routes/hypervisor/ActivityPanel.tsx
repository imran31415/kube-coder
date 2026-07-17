import { useEffect, useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { getThreadActivity, type ActivityEntry, type ThreadActivity } from '../../api/hypervisor';
import { fmtDuration, entryTone, entryLabel, totalErrors, clip } from './activity';

/**
 * Collapsible observability panel for the active hypervisor thread (issue:
 * hypervisor activity/logs). Shows a normalized tool timeline (calls + results
 * + durations, errors, status changes) derived server-side from events.jsonl,
 * plus a toggle to reveal the tail of the runner.log (the subprocess stderr /
 * runner diagnostics that were previously dropped). Lets a user see what the
 * agent is doing — and why a turn stalled or failed — without attaching to
 * tmux/stderr.
 *
 * Collapsed and idle by default: it only polls the /activity endpoint while
 * open, at a faster cadence while a turn is running so it feels live.
 */
export function ActivityPanel({ threadId, running }: { threadId: string; running: boolean }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ThreadActivity | null>(null);
  const [showLog, setShowLog] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Reset when switching threads so we never show one thread's activity on
  // another.
  useEffect(() => {
    setData(null);
    setErr(null);
  }, [threadId]);

  // Poll only while open. Faster while the turn runs; skip when the tab is
  // hidden. Fail-safe: a fetch error is shown but keeps the last data.
  useEffect(() => {
    if (!open || !threadId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (typeof document === 'undefined' || !document.hidden) {
        try {
          const d = await getThreadActivity(threadId);
          if (!cancelled) {
            setData(d);
            setErr(null);
          }
        } catch (e) {
          if (!cancelled) setErr(e instanceof Error ? e.message : 'Failed to load activity');
        }
      }
      if (!cancelled) timer = setTimeout(tick, running ? 3000 : 8000);
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [open, threadId, running]);

  const counts = data?.counts;
  const errs = totalErrors(counts);

  return (
    <div class="hv-ap">
      <button
        type="button"
        class="hv-ap-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Icon name="tasks" size={13} />
        <span class="hv-ap-title">Activity</span>
        {counts && (
          <span class="hv-ap-badges">
            <span class="hv-ap-badge">{counts.tool_calls} {counts.tool_calls === 1 ? 'tool' : 'tools'}</span>
            {errs > 0 && <span class="hv-ap-badge hv-ap-badge--err">{errs} {errs === 1 ? 'error' : 'errors'}</span>}
          </span>
        )}
        <Icon name="chevron-down" size={13} class={`hv-ap-caret${open ? ' hv-ap-caret--open' : ''}`} />
      </button>

      {open && (
        <div class="hv-ap-body">
          {err && <div class="hv-ap-err">{err}</div>}
          {!data && !err && <div class="hv-ap-empty">Loading…</div>}
          {data && data.timeline.length === 0 && !err && (
            <div class="hv-ap-empty">No tool activity yet.</div>
          )}
          {data && data.timeline.length > 0 && (
            <ol class="hv-ap-timeline">
              {data.timeline.map((e) => (
                <ActivityRow key={`${e.kind}-${e.seq}`} e={e} />
              ))}
            </ol>
          )}

          <div class="hv-ap-logbar">
            <button
              type="button"
              class="hv-ap-logtoggle"
              onClick={() => setShowLog((v) => !v)}
              aria-expanded={showLog}
            >
              <Icon name="terminal" size={12} /> {showLog ? 'Hide' : 'Show'} runner log
            </button>
          </div>
          {showLog && (
            <pre class="hv-ap-log">{data?.runner_log?.trim() || '(runner log is empty)'}</pre>
          )}
        </div>
      )}
    </div>
  );
}

function ActivityRow({ e }: { e: ActivityEntry }) {
  const tone = entryTone(e);
  const label = entryLabel(e);
  const detail =
    e.kind === 'error'
      ? clip(e.text)
      : e.kind === 'tool' && e.status === 'error'
        ? clip(e.result_text)
        : e.kind === 'tool_result_orphan'
          ? clip(e.result_text)
          : '';
  return (
    <li class={`hv-ap-row hv-ap-row--${e.kind}`}>
      <span class={`hv-ap-dot hv-ap-dot--${tone}`} aria-hidden="true" />
      <span class="hv-ap-label">{label}</span>
      {e.kind === 'tool' && e.status === 'pending' && (
        <span class="hv-ap-running">running…</span>
      )}
      {e.kind === 'tool' && e.duration_ms != null && (
        <span class="hv-ap-dur">{fmtDuration(e.duration_ms)}</span>
      )}
      {detail && <span class="hv-ap-detail">{detail}</span>}
    </li>
  );
}
