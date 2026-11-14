import { useEffect, useState } from 'preact/hooks';
import type { Problem } from '../api/problem';

interface Props {
  problem: Problem | null;
  refreshing: boolean;
  updatedAt: number | null;
  /** The data on screen is an earlier reading, kept because this one failed. */
  stale?: boolean;
  retry: () => void;
}

export function ResourceStatus({ problem, refreshing, updatedAt, stale, retry }: Props) {
  const [copied, setCopied] = useState(false);
  const [selectable, setSelectable] = useState(false);
  const reference = problem?.error_id || problem?.request_id;
  useEffect(() => { setCopied(false); setSelectable(false); }, [reference]);
  async function copy() {
    try { await navigator.clipboard.writeText(reference!); setCopied(true); }
    catch { setSelectable(true); }
  }
  if (!problem) return refreshing && !updatedAt ? <div class="panel-msg" role="status">Loading metrics…</div> : null;
  const setup = ['metrics_disabled', 'metrics_not_reporting'].includes(problem.code);
  return <div class="resource-status" role="status" aria-live="polite">
    <div class="resource-status-message">{problem.error}</div>
    {stale && updatedAt && <div class="resource-status-meta">Showing previous data · Updated {new Date(updatedAt).toLocaleTimeString()}</div>}
    <div class="resource-status-actions">
      {(problem.retryable || setup) && <button type="button" class="chip" disabled={refreshing} onClick={retry}>
        {refreshing ? 'Retrying…' : setup ? 'Check again' : 'Retry'}
      </button>}
      {problem.code === 'unauthorized' && <a class="panel-link" href="/oauth2/start">Sign in</a>}
      {problem.code === 'network_error' && <a class="panel-link" href="/oauth2/start">Sign in if your session expired</a>}
      {reference && <button type="button" class="chip" onClick={copy}>{copied ? 'Copied' : 'Copy reference'}</button>}
      {selectable && <code class="resource-reference">{reference}</code>}
    </div>
    {setup && <details><summary>Setup guidance</summary>
      <p>Ask your cluster operator to check monitoring configuration, workspace scrape credentials and target selection.</p>
      <a class="panel-link" href="https://github.com/imran31415/kube-coder/blob/main/docs/platform-observability.md" target="_blank" rel="noreferrer">Monitoring setup</a>
    </details>}
  </div>;
}
