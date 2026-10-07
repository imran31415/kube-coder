import { useEffect, useRef, useState } from 'preact/hooks';
import { ApiError } from '../../api/client';
import { getPublish, getPublishDiff, publishAction, publishing, type PublishStatus } from '../../api/publish';
import './publish.css';

export function PublishPanel({ taskId }: { taskId: string }) {
  return <Review key={taskId} taskId={taskId} />;
}
function Review({ taskId }: { taskId: string }) {
  const [status, setStatus] = useState<PublishStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [draft, setDraft] = useState(false);
  const [remote, setRemote] = useState('');
  const [base, setBase] = useState('');
  const [diff, setDiff] = useState<{ file: string; text: string } | null>(null);
  const loaded = useRef('');
  const editRevision = useRef(0);
  const alive = useRef(true);
  const working = useRef(false);
  const fileRequest = useRef(0);
  function accept(s: PublishStatus, ownSave = false) {
    if (!alive.current) return;
    setStatus(s);
    const p = s.preparation;
    if (p && (loaded.current !== p.id || ownSave)) {
      loaded.current = p.id; editRevision.current = p.draft_revision;
      setTitle(p.title); setBody(p.body); setDraft(p.draft); setDiff(null);
    }
  }
  useEffect(() => {
    alive.current = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { if (!working.current) accept(await getPublish(taskId)); }
      catch (e) { if (alive.current && !(e instanceof ApiError && e.status === 404)) setError(e instanceof Error ? e.message : String(e)); }
      finally { if (alive.current) timer = setTimeout(poll, 2500); }
    };
    void poll();
    return () => { alive.current = false; clearTimeout(timer); };
  }, [taskId]);
  async function act(fn: () => Promise<PublishStatus>) {
    if (working.current) return;
    working.current = true; setBusy(true); setError('');
    try { accept(await fn(), true); }
    catch (e) { if (alive.current) setError(e instanceof Error ? e.message : 'Connection interrupted. Reopen this Build to check saved progress.'); }
    finally { working.current = false; if (alive.current) setBusy(false); }
  }
  if (!status?.supported) return error ? <p role="alert">{error}</p> : null;
  const p = status.preparation;
  const active = publishing(status) || busy || !!status.preparing;
  const changed = p && (p.title !== title || p.body !== body || p.draft !== draft);
  const saved = () => publishAction(taskId, 'draft', { preparation_id: p!.id, draft_revision: editRevision.current, title, body, draft });
  async function submit() {
    const s = changed ? await saved() : status!;
    accept(s, true);
    const ready = s.preparation!;
    return publishAction(taskId, '', { preparation_id: ready.id, fingerprint: ready.fingerprint, draft_revision: ready.draft_revision, idempotency_key: crypto.randomUUID() });
  }
  return <section class="publish-panel" aria-label="Publish Build">
    <strong>Publish this Build</strong>
    <p>Review the changes, then commit this snapshot and open a GitHub pull request.</p>
    {status.eligibility.reason && <p role="status">{status.eligibility.reason.message}</p>}
    {(error || status.preparation_error?.message) && <p role="alert">{error || status.preparation_error?.message}</p>}
    {status.pr && <a class="publish-action" href={status.pr.url} target="_blank" rel="noopener noreferrer">Open PR #{status.pr.number} · {status.pr.state}</a>}
    {status.operation && <p role="status" aria-live="polite">Publishing: {status.operation.stage.replaceAll('_', ' ')}{status.operation.commit_sha && ` · ${status.operation.commit_sha.slice(0, 7)}`}</p>}
    {status.operation?.error && <p role="alert">{status.operation.error.message}</p>}
    {status.operation?.stage === 'failed' && <button disabled={active} onClick={() => void act(() => publishAction(taskId, `${status.operation!.id}/retry`))}>Retry saved publish</button>}
    <details><summary>Destination options</summary>
      <label>Push remote<input value={remote} placeholder="Use project configuration" onInput={e => setRemote(e.currentTarget.value)} disabled={active} /></label>
      <label>Base branch<input value={base} placeholder="Use Build base" onInput={e => setBase(e.currentTarget.value)} disabled={active} /></label>
    </details>
    <button disabled={active || !status.eligibility.can_prepare} onClick={() => void act(() => publishAction(taskId, 'prepare', { destination: { remote, base_branch: base } }))}>{status.preparing ? 'Preparing review…' : p ? 'Refresh review' : 'Prepare review'}</button>
    {p && <>
      <p>{p.destination.head_repo}:{p.destination.head_branch} → {p.destination.base_repo}:{p.destination.base_branch}</p>
      <p>Git author: {p.author.name} &lt;{p.author.email}&gt; · GitHub: {p.destination.identity}</p>
      <p>Tests: {p.checks.state}. Check the Build log before publishing.</p>
      {p.summary_error && <p role="status">{p.summary_error}</p>}
      <details><summary>Reviewed snapshot · {p.files.length} files</summary>
        {p.files.map(f => <button key={f.path} class="publish-file" onClick={async () => {
          const n = ++fileRequest.current;
          setDiff({ file: f.path, text: 'Loading…' });
          try { const r = await getPublishDiff(taskId, p.id, f.path); if (alive.current && n === fileRequest.current) setDiff({ file: f.path, text: r.diff + (r.truncated ? '\n[Diff truncated]' : '') }); }
          catch (e) { if (alive.current && n === fileRequest.current) setDiff({ file: f.path, text: String(e) }); }
        }}>{f.path} {f.binary ? '(binary)' : `+${f.added} −${f.deleted}`}</button>)}
        {diff && <div><strong>{diff.file}</strong><pre tabIndex={0}>{diff.text}</pre></div>}
      </details>
      <label>PR title<input value={title} maxLength={200} disabled={active} onInput={e => setTitle(e.currentTarget.value)} /></label>
      <label>PR summary<textarea value={body} maxLength={20000} rows={7} disabled={active} onInput={e => setBody(e.currentTarget.value)} /></label>
      <label class="publish-check"><input type="checkbox" checked={draft} disabled={active} onChange={e => setDraft(e.currentTarget.checked)} />Create as draft PR</label>
      <button disabled={active || !changed || !title.trim()} onClick={() => void act(saved)}>Save description</button>
      <div class="publish-footer">{status.pr && status.operation?.stage === 'published' && status.operation.preparation_id === p.id
        ? <a class="publish-action" href={status.pr.url} target="_blank" rel="noopener noreferrer">View PR</a>
        : <button disabled={active || !title.trim() || !status.eligibility.can_publish} onClick={() => void act(submit)}>{publishing(status) ? 'Publishing…' : status.pr ? 'Push updates' : 'Push & Open PR'}</button>}</div>
    </>}
  </section>;
}
