import { useEffect, useRef, useState } from 'preact/hooks';
import { signupRequest, SignupFailure, type SignupMe, type SignupOperation, type SignupProblem } from '../api/signup';

const stages: Record<SignupOperation['stage'], string> = {
  identity: 'Preparing sign-in', configuration: 'Saving configuration',
  deployment: 'Starting workspace', readiness: 'Checking readiness', ready: 'Your workspace is ready',
  failed: 'Workspace setup needs attention', deleted: 'Your workspace was removed',
};

export function SignupPage() {
  const [me, setMe] = useState<SignupMe | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<SignupProblem | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [checked, setChecked] = useState<number | null>(null);
  const inFlight = useRef(false);
  const submitting = useRef(false);
  const mounted = useRef(true);
  const operation = me?.operation;
  const active = operation && !['ready', 'failed', 'deleted'].includes(operation.stage);

  async function refresh(signal?: AbortSignal) {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const result = await signupRequest<SignupMe>('me', undefined, signal);
      if (signal?.aborted || !mounted.current) return;
      setMe(result); setSignedOut(false); setError(null); setUncertain(false); setChecked(Date.now());
    } catch (e) {
      if (signal?.aborted || !mounted.current) return;
      const failure = e as SignupFailure;
      if (failure.status === 401) { setSignedOut(true); setMe(null); }
      else setError(failure.problem);
    } finally {
      inFlight.current = false;
      if (mounted.current) setLoading(false);
    }
  }

  useEffect(() => {
    mounted.current = true;
    const abort = new AbortController();
    void refresh(abort.signal);
    return () => { mounted.current = false; abort.abort(); };
  }, []);

  useEffect(() => {
    if (!active && !uncertain) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let delay = 3000;
    async function poll() {
      await refresh(abort.signal);
      if (!abort.signal.aborted) {
        delay = Math.min(delay * 1.3, 15000);
        timer = setTimeout(poll, delay);
      }
    }
    timer = setTimeout(poll, delay);
    return () => { abort.abort(); clearTimeout(timer); };
  }, [active, uncertain]);

  async function submit(retry: boolean) {
    if (!me || submitting.current || uncertain) return;
    submitting.current = true;
    setBusy(true); setError(null);
    try {
      const result = await signupRequest<{ operation: SignupOperation }>(retry ? 'retry' : 'workspace', me.csrfToken);
      if (mounted.current) setMe({ ...me, operation: result.operation });
    } catch (e) {
      const failure = e as SignupFailure;
      if (mounted.current) {
        setError(failure.problem);
        // A lost response does not mean creation failed. Observe before replay.
        if (failure.status === 0) setUncertain(true);
        if (failure.status === 401) setSignedOut(true);
      }
    } finally { submitting.current = false; if (mounted.current) setBusy(false); }
  }

  const problem = error || operation?.error;
  return <main class="signup-shell">
    <div class="signup-brand">Kube Coder</div>
    <section class="signup-card" aria-busy={loading || busy}>
      <p class="signup-eyebrow">YOUR DEVELOPMENT SPACE</p>
      <h1>{operation ? stages[operation.stage] : 'A workspace of your own'}</h1>
      <p class="sub">Sign in with GitHub. We’ll take care of setting up your workspace.</p>
      {loading && <p role="status">Checking your session…</p>}
      {!loading && signedOut && <a class="btn start signup-primary" href="/oauth2/start?rd=%2Fsignup">Continue with GitHub</a>}
      {!loading && me && !signedOut && <>
        <div class="signup-account">Signed in as <strong>@{me.login}</strong><a href="/oauth2/sign_out?rd=%2Fsignup">Sign out</a></div>
        {!me.eligible && !operation && <div role="status"><h2>You’re signed in</h2><p>Your GitHub account isn’t approved yet. Ask your administrator to enable access.</p><button class="btn" onClick={() => void refresh()}>Check again</button></div>}
        {me.eligible && !operation && <button class="btn start signup-primary" disabled={busy || uncertain} onClick={() => void submit(false)}>{busy ? 'Starting…' : 'Create workspace'}</button>}
        {operation && <div aria-live="polite" role="status">
          <p class="signup-workspace">{operation.slug}</p>
          {active && <ol class="signup-stages">{(['identity', 'configuration', 'deployment', 'readiness'] as const).map(stage => <li key={stage} aria-current={stage === operation.stage ? 'step' : undefined}>{stages[stage]}</li>)}</ol>}
          {active && Date.now() / 1000 - (operation.startedAt ?? operation.updatedAt) > 45 && <p>Setup is taking longer than usual. You can leave this page and return later.</p>}
          {operation.stage === 'ready' && operation.url && <a class="btn start signup-primary" href={operation.url}>Open workspace →</a>}
          {operation.stage === 'deleted' && me.eligible && <button class="btn" disabled={busy} onClick={() => void submit(true)}>Create workspace again</button>}
          {operation.stage === 'failed' && operation.error?.retryable && me.eligible && <button class="btn" disabled={busy || uncertain} onClick={() => void submit(true)}>Retry setup</button>}
        </div>}
      </>}
      {problem && <div class="signup-error" role="alert"><p>{problem.error}</p>{problem.error_id && <button class="btn ghost" onClick={() => { void navigator.clipboard?.writeText(problem.error_id!).catch(() => {}); }}>Copy reference: {problem.error_id}</button>}<button class="btn ghost" onClick={() => void refresh()}>Check again</button></div>}
      {uncertain && <p role="status">Checking whether setup started. Please don’t submit again.</p>}
      {checked && <p class="signup-updated">Last checked {new Date(checked).toLocaleTimeString()}</p>}
    </section>
    <p class="signup-footnote">Private by default. Only your GitHub account can open your workspace.</p>
  </main>;
}
