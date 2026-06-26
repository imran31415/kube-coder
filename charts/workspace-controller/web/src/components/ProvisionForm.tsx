import { useEffect, useState } from 'preact/hooks';
import { navigate, provisionError, provisionSlug, route } from '../router';
import { provisionConfig } from '../store';
import {
  type ProvisionStatus,
  type ValidateUserResponse,
  deployExisting,
  getProvisionStatus,
  startManifest,
  submitManifestToGithub,
  validateUser,
} from '../api/provision';

// Top-level provision view: the create form, or — once GitHub has redirected
// back to #/provision/<slug> — the live status poller.
export function ProvisionForm() {
  const path = route.value;
  const slug = provisionSlug(path);
  return slug ? <ProvisionStatusView slug={slug} /> : <ProvisionCreate initialError={provisionError(path)} />;
}

function Header({ subtitle }: { subtitle: string }) {
  return (
    <header class="hdr">
      <div>
        <h1>New workspace</h1>
        <p class="sub">{subtitle}</p>
      </div>
      <button class="btn ghost" onClick={() => navigate('/')}>
        ← Workspaces
      </button>
    </header>
  );
}

function ProvisionCreate({ initialError }: { initialError: string | null }) {
  const cfg = provisionConfig.value;
  const [login, setLogin] = useState('');
  const [validating, setValidating] = useState(false);
  const [info, setInfo] = useState<ValidateUserResponse | null>(null);
  const [err, setErr] = useState<string | null>(initialError);
  const [submitting, setSubmitting] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [pvcSize, setPvcSize] = useState('20Gi');
  const [gitName, setGitName] = useState('');
  const [gitEmail, setGitEmail] = useState('');

  if (cfg && !cfg.enabled) {
    return (
      <div class="app">
        <Header subtitle="Provisioning is not configured on this controller." />
        <div class="banner err" role="alert">
          Set <code>provision.enabled</code> and its credentials in the controller chart to enable this.
        </div>
      </div>
    );
  }

  async function onValidate(e?: Event) {
    e?.preventDefault();
    const user = login.trim();
    if (!user) return;
    setValidating(true);
    setErr(null);
    setInfo(null);
    try {
      const res = await validateUser(user);
      setInfo(res);
      setGitName(res.name || res.login);
      setGitEmail(res.email || '');
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : String(e2));
    } finally {
      setValidating(false);
    }
  }

  async function onProvision() {
    if (!info) return;
    setSubmitting(true);
    setErr(null);
    try {
      const m = await startManifest({
        user: info.login,
        pvcSize: pvcSize.trim() || undefined,
        gitName: gitName.trim() || undefined,
        gitEmail: gitEmail.trim() || undefined,
      });
      // Navigates the browser to GitHub's "Create GitHub App" confirmation.
      submitManifestToGithub(m);
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : String(e2));
      setSubmitting(false);
    }
  }

  // The GitHub App + config already exist (e.g. a retry after a failed Job):
  // skip the manifest detour and relaunch the deploy Job from saved config.
  async function onDeployExisting() {
    if (!info) return;
    setSubmitting(true);
    setErr(null);
    try {
      await deployExisting(info.slug);
      navigate(`/provision/${info.slug}`);
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : String(e2));
      setSubmitting(false);
    }
  }

  const domainHint = cfg?.workspaceDomain ? `<username>.${cfg.workspaceDomain}` : 'the workspace domain';

  return (
    <div class="app">
      <Header subtitle={`Enter a GitHub username. We register a GitHub App for them, then deploy a workspace at ${domainHint}.`} />

      {err && (
        <div class="banner err" role="alert">
          {err}
        </div>
      )}

      <form class="prov-form" onSubmit={onValidate}>
        <label class="field">
          <span class="field-label">GitHub username</span>
          <div class="field-row">
            <input
              class="input"
              type="text"
              autocomplete="off"
              autocapitalize="none"
              spellcheck={false}
              placeholder="octocat"
              value={login}
              onInput={(e) => {
                setLogin((e.target as HTMLInputElement).value);
                setInfo(null);
              }}
            />
            <button class="btn" type="submit" disabled={validating || !login.trim()}>
              {validating ? '…' : 'Look up'}
            </button>
          </div>
        </label>
      </form>

      {info && (
        <div class="prov-preview">
          <div class="prov-user">
            {info.avatarUrl && <img class="prov-avatar" src={info.avatarUrl} alt="" width={40} height={40} />}
            <div>
              <div class="prov-name">{info.name}</div>
              <div class="row-meta">
                @{info.login} · workspace host <code>{info.host}</code>
              </div>
            </div>
          </div>

          {info.exists && (
            <div class="banner warn" role="alert">
              A workspace <code>ws-{info.slug}</code> already exists — provisioning will re-deploy it.
            </div>
          )}

          <button class="prov-toggle" type="button" onClick={() => setAdvanced((v) => !v)}>
            {advanced ? '▾' : '▸'} Advanced options
          </button>
          {advanced && (
            <div class="prov-advanced">
              <label class="field">
                <span class="field-label">Disk size</span>
                <input class="input" value={pvcSize} onInput={(e) => setPvcSize((e.target as HTMLInputElement).value)} />
              </label>
              <label class="field">
                <span class="field-label">Git author name</span>
                <input class="input" value={gitName} onInput={(e) => setGitName((e.target as HTMLInputElement).value)} />
              </label>
              <label class="field">
                <span class="field-label">Git author email</span>
                <input class="input" value={gitEmail} onInput={(e) => setGitEmail((e.target as HTMLInputElement).value)} />
              </label>
            </div>
          )}

          {info.configExists ? (
            <>
              <p class="sub">
                A GitHub App named <code>kube-coder-{info.slug}</code> is already registered and its
                config is saved — no GitHub step needed. Deploy straight from the saved config.
              </p>
              <button class="btn start prov-go" type="button" disabled={submitting} onClick={onDeployExisting}>
                {submitting ? 'Starting…' : 'Deploy workspace'}
              </button>
            </>
          ) : (
            <>
              <p class="sub">
                Next: GitHub opens to confirm a new App named <code>kube-coder-{info.slug}</code>. Click{' '}
                <strong>Create GitHub App</strong> and you'll be returned here to watch the rollout.
              </p>
              <button class="btn start prov-go" type="button" disabled={submitting} onClick={onProvision}>
                {submitting ? 'Opening GitHub…' : 'Register GitHub App & provision'}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ProvisionStatusView({ slug }: { slug: string }) {
  const [status, setStatus] = useState<ProvisionStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);

  useEffect(() => {
    let live = true;
    async function poll() {
      try {
        const s = await getProvisionStatus(slug);
        if (live) setStatus(s);
      } catch (e) {
        if (live) setErr(e instanceof Error ? e.message : String(e));
      }
    }
    void poll();
    const id = window.setInterval(poll, 3000);
    return () => {
      live = false;
      window.clearInterval(id);
    };
  }, [slug]);

  const job = status?.job ?? 'pending';
  // A failed provisioner Job must win over a still-running pod: a half-applied
  // deploy (pod up, but helm/RBAC failed mid-rollout) is NOT "ready".
  const ready = status?.workspace?.state === 'running' && job !== 'failed';
  const partial = job === 'failed' && status?.workspace?.state === 'running';
  const phase = jobPhase(job, ready, partial);

  // Relaunch the deploy Job from the saved GitOps config — no GitHub step, no
  // re-typing. The poll loop picks up the new Job state from here.
  async function onRetry() {
    setRetrying(true);
    setErr(null);
    try {
      setStatus(await deployExisting(slug));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setRetrying(false);
    }
  }

  return (
    <div class="app">
      <Header subtitle={`Provisioning ${slug}`} />
      {err && (
        <div class="banner err" role="alert">
          {err}
        </div>
      )}
      <div class="prov-status">
        <div class={`prov-stage stage-${phase.tone}`}>
          <span class="prov-spinner" aria-hidden="true">
            {phase.tone === 'progress' ? '◐' : phase.tone === 'ok' ? '✓' : '✕'}
          </span>
          <div>
            <div class="prov-stage-title">{phase.title}</div>
            <div class="row-meta">{status?.message || phase.detail}</div>
          </div>
        </div>

        {ready && status && (
          <a class="btn start prov-go" href={status.url} target="_blank" rel="noopener">
            Open {slug} ↗
          </a>
        )}
        {job === 'failed' && (
          <div class="prov-actions">
            <button class="btn start prov-go" type="button" disabled={retrying} onClick={onRetry}>
              {retrying ? 'Retrying…' : partial ? 'Finish deploy' : 'Retry deploy'}
            </button>
            <button class="btn ghost" type="button" onClick={() => navigate('/provision')}>
              Start over
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function jobPhase(job: ProvisionStatus['job'], ready: boolean, partial = false): { title: string; detail: string; tone: 'progress' | 'ok' | 'err' } {
  if (ready) return { title: 'Workspace ready', detail: 'The pod is running.', tone: 'ok' };
  switch (job) {
    case 'succeeded':
      return { title: 'Deployed — waiting for pod', detail: 'Helm finished; the workspace is starting.', tone: 'progress' };
    case 'running':
      return { title: 'Deploying workspace', detail: 'Running helm upgrade…', tone: 'progress' };
    case 'failed':
      return partial
        ? { title: 'Provisioning incomplete', detail: 'The pod is up but the deploy failed mid-rollout — click Finish deploy to complete it.', tone: 'err' }
        : { title: 'Provisioning failed', detail: 'See the provisioner Job logs.', tone: 'err' };
    default:
      return { title: 'Starting provisioner', detail: 'Launching the deploy Job…', tone: 'progress' };
  }
}
