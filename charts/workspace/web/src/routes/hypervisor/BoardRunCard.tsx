import { useEffect, useMemo, useState } from 'preact/hooks';
import type { BoardRequest } from '../../api/boardRequests';
import { createBoardRequestsStore, type BoardRequestsStore } from '../../store/boardRequests';
import { navigate, routeHref } from '../../store/router';
import { boardRunPath } from '../board/runLinks';
import { serverMode } from '../../store/server-mode';
import './boardRunCard.css';

export function BoardRunCards({ threadId }: { threadId: string }) {
  const store = useMemo(() => createBoardRequestsStore(threadId), [threadId]);
  useEffect(() => store.start(), [store]);
  if (!store.requests.value.length && !store.error.value) return null;
  return <section class="hv-board-cards" aria-label="Board requests">
    {store.error.value && <div class="hv-board-error" role="alert">
      <span>{store.error.value}</span><button onClick={() => void store.refresh()}>Refresh status</button>
    </div>}
    {store.requests.value.slice(0, 10).map(record => <BoardRunCard key={record.id} record={record} store={store} />)}
  </section>;
}

export function BoardRunCard({ record, store }: { record: BoardRequest; store: BoardRequestsStore }) {
  const [editing, setEditing] = useState(false);
  const [limit, setLimit] = useState(record.scope.options.select?.limit ?? 3);
  const [workers, setWorkers] = useState(record.scope.options.concurrency ?? 1);
  const pending = record.state === 'pending';
  const canDecide = pending && !serverMode.value.readOnly;
  const busy = store.busy.value !== null;
  const stop = record.scope.operation === 'stop';
  const run = record.run;
  const heading = {
    pending: stop ? 'Stop Board run — approval needed' : 'Board run — approval needed',
    rejected: 'Board request cancelled', expired: 'Board request expired', starting: 'Starting Board run…',
    started: stop ? 'Stop requested' : run?.status === 'running' ? 'Passed to Board' :
      run?.status === 'interrupted' ? 'Board run interrupted' : run?.status === 'stopped' ? 'Board run stopped' : 'Board work finished',
    failed: 'Board could not start', interrupted: 'Board startup needs attention',
  }[record.state];
  const path = record.run_id && record.run ? boardRunPath(record.board_id, record.run_id) : null;
  return <article class="hv-board-card" aria-labelledby={`board-title-${record.id}`}>
    <div class="hv-board-heading"><span class={`hv-board-dot hv-board-dot--${record.state}`} />
      <h3 id={`board-title-${record.id}`}>{heading}</h3></div>
    <p class="hv-board-target"><strong>{record.scope.board_name}</strong><span>{record.scope.repository.replace('https://', '')}</span></p>
    {!stop && <p class="hv-board-scope">Up to {record.scope.options.select.limit} items · {record.scope.options.concurrency} worker{record.scope.options.concurrency === 1 ? '' : 's'} · Propose · Isolated code</p>}
    {pending && <p>{stop ? 'Stop starting new items. Running items will finish.' :
      'Reads matching Board items and uses your coding agent to work and commit locally. Tracker changes need separate review. No push or merge is approved.'}</p>}
    {record.state === 'starting' && <p role="status">Checking access and matching items. You can keep chatting.</p>}
    {run && <p class="hv-board-counts">{run.counts.working + run.counts.claimed} working · {run.counts.pending} waiting · {run.failed} failed · {run.skipped} skipped</p>}
    {run && <p>{record.review_needed ?? 0} item{record.review_needed === 1 ? ' needs' : 's need'} review.</p>}
    {run && run.total === 0 && run.status === 'done' && <p>No matching unprocessed items were found.</p>}
    {run && run.status !== 'running' && <p>Review the item outcomes in Board before accepting proposed changes.</p>}
    {record.error.error && <p class="hv-board-error" role="alert">{record.error.error}</p>}
    {!pending && store.lastChecked.value && <small>Last checked {new Date(store.lastChecked.value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</small>}
    <details><summary>Scope details</summary><dl>
      <dt>Checkout</dt><dd>{record.scope.options.workdir || 'Existing run'}</dd>
      <dt>Base</dt><dd>{record.scope.options.base_ref || 'Repository default'}</dd>
      <dt>Selection</dt><dd><code>{JSON.stringify(record.scope.options.select ?? {})}</code></dd>
    </dl></details>
    {pending && !canDecide && <p>Open a writable workspace to review this request.</p>}
    {editing && canDecide && <form class="hv-board-adjust" onSubmit={event => {
      event.preventDefault(); void store.adjust(record, limit, workers).then(() => setEditing(false));
    }}>
      <label>Maximum items<input type="number" min={1} max={500} required value={limit} onInput={e => setLimit(Number(e.currentTarget.value))} /></label>
      <label>Workers<input type="number" min={1} max={8} required value={workers} onInput={e => setWorkers(Number(e.currentTarget.value))} /></label>
      <button disabled={busy} type="submit">Prepare updated request</button>
      <button type="button" onClick={() => setEditing(false)}>Keep current scope</button>
    </form>}
    <div class="hv-board-actions">
      {canDecide && !editing && <>
        <button class="hv-board-primary" disabled={busy} onClick={() => void store.decide(record, 'approve')}>{busy ? 'Saving decision…' : stop ? 'Approve stop' : 'Approve & run'}</button>
        {!stop && <button disabled={busy} onClick={() => setEditing(true)}>Adjust</button>}
        <button disabled={busy} onClick={() => void store.decide(record, 'reject')}>Cancel</button>
      </>}
      {path && <a href={routeHref(path)} onClick={event => { if (!event.ctrlKey && !event.metaKey) { event.preventDefault(); navigate(path); } }}>Open Board</a>}
      {(record.state === 'expired' || record.state === 'failed') && <span>Ask the agent to prepare a new request.</span>}
    </div>
  </article>;
}
