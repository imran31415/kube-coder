import { useEffect, useState } from 'preact/hooks';
import { listTriggerRuns, type Trigger, type TriggerRun } from '../../api/triggers';
import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { navigate, routeHref } from '../../store/router';

/** How many entries one fetch pulls. Small on purpose: the panel opens inside a
 *  list row, and the question it answers ("did the last few fires work?") is
 *  almost always answered by the first page. */
const PAGE = 20;

/** Per-outcome presentation. A table rather than nested ternaries, for the same
 *  reason KIND_META is one: an outcome the server adds later renders as itself
 *  instead of silently taking the last branch. */
const OUTCOME_META: Record<
  string,
  { label: string; tone: 'success' | 'warn' | 'danger' | 'neutral' }
> = {
  spawned: { label: 'task started', tone: 'success' },
  skipped: { label: 'nothing to do', tone: 'neutral' },
  rejected: { label: 'rejected', tone: 'warn' },
  error: { label: 'error', tone: 'danger' },
};

/** Plain English for the ledger's reason slugs. An unmapped slug falls through
 *  as itself, which is worse-looking but never wrong. */
const REASON_TEXT: Record<string, string> = {
  bad_signature: 'signature did not match',
  bad_token: 'fire token did not match',
  replay: 'duplicate body (replay)',
  invalid_payload: 'body was not valid JSON',
  payload_too_large: 'body over the 1 MiB cap',
  at_capacity: 'workspace was at its task limit',
  suspended: 'trigger is paused',
  spawn_failed: 'the task could not be started',
  fetch_failed: 'the page could not be read',
  unchanged: 'page had not changed',
  baseline: 'first check - recorded a baseline',
  busy: 'a check was already running',
  changed: 'page changed',
};

/** Compact local time — "Sep 20, 16:28". Deliberately not the full
 *  `toLocaleString()`: at five columns inside a list row this is the widest
 *  cell, and on a phone it pushes Source and Task off the visible strip. The
 *  full stamp, seconds and all, stays available as the cell's title. */
function when(ts: number): { text: string; title: string } {
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return { text: '-', title: '' };
  return {
    text: d.toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      hour12: false,
    }),
    title: d.toLocaleString(),
  };
}

/** Where the call came from, as one short string. The forwarded hop is shown in
 *  preference to the socket peer because behind the workspace ingress the peer
 *  is the ingress controller on every single row — but it is labelled, because
 *  anyone can send the header. */
function origin(run: TriggerRun): { text: string; title: string } {
  if (run.manual) return { text: 'dashboard', title: 'Fired from this dashboard' };
  if (run.forwarded_for) {
    return {
      text: run.forwarded_for,
      title: `X-Forwarded-For (asserted by the caller). Socket peer: ${run.source_ip || 'unknown'}`,
    };
  }
  if (run.source_ip) return { text: run.source_ip, title: 'Socket peer address' };
  return { text: '-', title: 'No source recorded' };
}

export function TriggerRuns({ t }: { t: Trigger }) {
  const [runs, setRuns] = useState<TriggerRun[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load(offset: number) {
    setLoading(true);
    try {
      const page = await listTriggerRuns(t.kind, t.id, { limit: PAGE, offset });
      // Append rather than replace, so "Show more" builds one list. Offset 0 is
      // also the refresh path, hence the slice rather than a blind concat.
      setRuns((prev) => [...prev.slice(0, offset), ...page.runs]);
      setTotal(page.total);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(0);
    // Re-fetch when the panel is pointed at a different trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t.kind, t.id]);

  return (
    <section class="trig-runs" aria-label={`Run history for ${t.id}`}>
      {error ? (
        <div class="trig-runs-msg" role="alert">
          Could not load run history: {error}
        </div>
      ) : loading && runs.length === 0 ? (
        <div class="trig-runs-msg muted">Loading run history…</div>
      ) : runs.length === 0 ? (
        <div class="trig-runs-msg muted">
          {t.kind === 'webhook'
            ? 'No calls yet. Every POST to this webhook is recorded here - including ones rejected for a bad signature.'
            : t.kind === 'cron'
              ? 'No fires yet. Every fire is recorded here, including ones refused because the workspace was busy.'
              : 'No checks yet. Every check is recorded here, including the ones that found no change.'}
        </div>
      ) : (
        <>
          <table class="trig-runs-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Outcome</th>
                <th scope="col">Verified</th>
                <th scope="col">Source</th>
                <th scope="col">Task</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run, i) => {
                const meta = OUTCOME_META[run.outcome] ?? {
                  label: run.outcome,
                  tone: 'neutral' as const,
                };
                const why = run.reason ? REASON_TEXT[run.reason] ?? run.reason : '';
                const src = origin(run);
                const at = when(run.ts);
                return (
                  <tr key={`${run.ts}-${i}`}>
                    <td class="mono trig-runs-when" title={at.title}>{at.text}</td>
                    <td>
                      <Pill tone={meta.tone}>{meta.label}</Pill>
                      {why && <span class="trig-runs-why muted"> {why}</span>}
                      {/* The server does not write an error that merely
                          restates the reason; this is the belt to that braces,
                          so an older ledger entry cannot print one fact twice. */}
                      {run.error && run.error.toLowerCase() !== why.toLowerCase() && (
                        <div class="trig-runs-err muted" title={run.error}>{run.error}</div>
                      )}
                    </td>
                    <td>
                      {run.signature_verified === undefined ? (
                        <span class="muted" title="No signature check applies to this fire">-</span>
                      ) : run.signature_verified ? (
                        <Pill tone="success">verified</Pill>
                      ) : (
                        <Pill tone="danger">unverified</Pill>
                      )}
                    </td>
                    <td class="mono trig-runs-src" title={src.title}>{src.text}</td>
                    <td>
                      {run.task_id ? (
                        <a
                          class="mono trig-runs-task"
                          href={routeHref(`/tasks/${encodeURIComponent(run.task_id)}`)}
                          onClick={(e) => {
                            // Let ⌘/ctrl-click and middle-click open a real tab.
                            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                            e.preventDefault();
                            navigate(`/tasks/${encodeURIComponent(run.task_id as string)}`);
                          }}
                        >
                          {run.task_id}
                        </a>
                      ) : (
                        <span class="muted">-</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div class="trig-runs-foot muted">
            <span>
              {runs.length} of {total} recorded {total === 1 ? 'run' : 'runs'}
              {total >= PAGE && '. Older entries age out once the ledger is full.'}
            </span>
            {runs.length < total && (
              <Button size="sm" variant="ghost" disabled={loading}
                      onClick={() => void load(runs.length)}>
                {loading ? 'Loading…' : 'Show older'}
              </Button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
