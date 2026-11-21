import { useEffect, useRef, useState } from 'preact/hooks';
import { navigate } from '../router';
import { fmtAgo } from '../format';
import type { Problem } from '../api/problem';
import {
  type AccessEntry,
  type AccessOverview,
  type AccessWorkspace,
  type GithubAccount,
  accessProblem,
  allowAccount,
  getAccessList,
  lookupAccount,
  removeAccount,
} from '../api/allowlist';
import { ResourceStatus } from './ResourceStatus';

// Who may create a workspace at public signup. Adding someone takes
// effect within seconds on both controller replicas; removing them stops new
// signups only — an existing workspace keeps working until it is deleted.

const LOGIN_RE = /^[a-zA-Z0-9](?:-?[a-zA-Z0-9]){0,38}$/;
const PAGE_SIZE = 20;
const SEARCH_FROM = 8;
const SLOW_MS = 8000;
// GitHub.com, like the rest of the controller (GITHUB_API in controller.py).
const GITHUB_WEB = 'https://github.com';
const GITHUB_AVATARS = 'https://avatars.githubusercontent.com';

export function SignupAccess() {
  const [data, setData] = useState<AccessOverview | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [loading, setLoading] = useState(true);
  const [slow, setSlow] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);
  const current = useRef<AbortController | null>(null);

  async function load() {
    current.current?.abort();
    const abort = new AbortController();
    current.current = abort;
    setLoading(true);
    setSlow(false);
    const slowTimer = setTimeout(() => setSlow(true), SLOW_MS);
    try {
      const result = await getAccessList(abort.signal);
      if (abort.signal.aborted) return;
      setData(result);
      setProblem(null);
      setUpdatedAt(Date.now());
    } catch (e) {
      if (!abort.signal.aborted) setProblem(accessProblem(e));
    } finally {
      clearTimeout(slowTimer);
      if (current.current === abort) {
        setLoading(false);
        setSlow(false);
      }
    }
  }

  useEffect(() => {
    void load();
    return () => current.current?.abort();
  }, []);

  function added(entry: AccessEntry) {
    setData((d) => d && { ...d, entries: [entry, ...d.entries.filter((e) => e.githubId !== entry.githubId)] });
    setFresh(entry.githubId);
    setNotice(`@${entry.login} can now sign up.`);
  }

  function removed(entry: AccessEntry) {
    setData((d) => d && { ...d, entries: d.entries.filter((e) => e.githubId !== entry.githubId) });
    setNotice(`${label(entry)} removed. ${entry.workspace ? 'Their existing workspace keeps working.' : ''}`.trim());
  }

  const host = hostOf(data?.signupUrl);
  return (
    <div class="app">
      <header class="hdr">
        <div>
          <button class="crumb" onClick={() => navigate('/')}>← Workspaces</button>
          <h1>Signup access</h1>
          <p class="sub">
            People who can create their own workspace{host ? <> at <strong>{host}</strong></> : ''}. Changes
            apply within seconds — no redeploy.
          </p>
        </div>
        <div class="hdr-actions">
          <button class="btn ghost" disabled={loading} onClick={() => void load()}>
            {loading && data ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      <AddPerson noteMax={data?.noteMax} onAdded={added} onUncertain={() => void load()} onStart={() => setNotice(null)} />

      {notice && (
        <div class="banner ok access-notice" role="status">
          <span>✓ {notice}</span>
          {data?.signupUrl && <CopyLink url={data.signupUrl} />}
        </div>
      )}

      {problem && (
        <ResourceStatus problem={problem} refreshing={loading} updatedAt={updatedAt} stale={!!data} retry={() => void load()} />
      )}

      {!data && loading && !problem && (
        <div class="empty loading" role="status">
          <span class="spinner" aria-hidden="true" />
          {slow ? (
            <span>
              Still loading — the controller is slow to answer.{' '}
              <button class="btn ghost" onClick={() => void load()}>Retry</button>
            </span>
          ) : (
            'Loading signup access…'
          )}
        </div>
      )}

      {data && <AccessList data={data} fresh={fresh} onRemoved={removed} onUncertain={() => void load()} />}
    </div>
  );
}

function AddPerson({ noteMax, onAdded, onUncertain, onStart }: {
  /** From the server; unknown until the list loads (the server checks it anyway). */
  noteMax?: number;
  onAdded: (entry: AccessEntry) => void;
  onUncertain: () => void;
  onStart: () => void;
}) {
  const [login, setLogin] = useState('');
  const [hint, setHint] = useState<string | null>(null);
  const [looking, setLooking] = useState(false);
  const [account, setAccount] = useState<GithubAccount | null>(null);
  const [note, setNote] = useState('');
  const [adding, setAdding] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const input = useRef<HTMLInputElement>(null);

  async function lookUp(e?: Event) {
    e?.preventDefault();
    const name = login.trim().replace(/^@/, '');
    if (!name || looking) return;
    if (!LOGIN_RE.test(name)) {
      setHint('GitHub usernames use letters, numbers and single hyphens.');
      return;
    }
    onStart();
    setLooking(true);
    setHint(null);
    setProblem(null);
    setAccount(null);
    try {
      setAccount(await lookupAccount(name));
      setNote('');
    } catch (e2) {
      setProblem(accessProblem(e2));
    } finally {
      setLooking(false);
    }
  }

  async function allow() {
    if (!account || adding) return;
    setAdding(true);
    setProblem(null);
    try {
      const { entry } = await allowAccount(account.login, account.githubId, note.trim());
      onAdded(entry);
      setAccount(null);
      setLogin('');
      setNote('');
      input.current?.focus();
    } catch (e2) {
      const p = accessProblem(e2);
      if (p.code === 'request_timeout') {
        // The write may still have landed: re-read instead of guessing.
        p.error += ' Refreshing the list to check.';
        onUncertain();
      }
      if (p.code === 'github_user_changed') setAccount(null);
      setProblem(p);
    } finally {
      setAdding(false);
    }
  }

  function cancel() {
    setAccount(null);
    setProblem(null);
    input.current?.focus();
  }

  return (
    <section class="access-add" aria-label="Add a person">
      <form class="field" onSubmit={lookUp}>
        <label class="field-label" for="access-login">Add a person by GitHub username</label>
        <div class="field-row">
          <input
            ref={input}
            id="access-login"
            class="input"
            type="text"
            autocomplete="off"
            autocapitalize="none"
            spellcheck={false}
            placeholder="octocat"
            value={login}
            disabled={looking}
            aria-invalid={!!hint}
            onInput={(e) => {
              setLogin((e.target as HTMLInputElement).value);
              setHint(null);
            }}
          />
          <button class="btn" type="submit" disabled={looking || !login.trim()}>
            {looking ? 'Looking up…' : 'Look up'}
          </button>
        </div>
        {hint && <div class="access-hint" role="alert">{hint}</div>}
      </form>

      {problem && <InlineProblem problem={problem} retry={account ? allow : lookUp} />}

      {account && (
        <div class="prov-preview" onKeyDown={(e) => e.key === 'Escape' && cancel()}>
          <div class="prov-user">
            <Avatar id={account.githubId} login={account.login} src={account.avatarUrl} />
            <div class="access-text">
              <div class="prov-name">{account.name || account.login}</div>
              <div class="row-meta">@{account.login} · ID {account.githubId}</div>
            </div>
            <WorkspaceLink workspace={account.workspace} quiet />
          </div>
          {account.allowed ? (
            <div class="access-actions">
              <span class="sub">
                {account.source === 'helm' ? 'Already allowed by the Helm values.' : 'Already on the list.'}
              </span>
              <button class="btn ghost" onClick={cancel}>Done</button>
            </div>
          ) : (
            <>
              <label class="field">
                <span class="field-label">Note (optional)</span>
                <input
                  class="input"
                  maxLength={noteMax}
                  placeholder="e.g. Spring intern"
                  value={note}
                  onInput={(e) => setNote((e.target as HTMLInputElement).value)}
                />
              </label>
              <div class="access-actions">
                <button class="btn start" disabled={adding} onClick={() => void allow()}>
                  {adding ? 'Allowing…' : `Allow @${account.login}`}
                </button>
                <button class="btn ghost" disabled={adding} onClick={cancel}>Cancel</button>
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}

function AccessList({ data, fresh, onRemoved, onUncertain }: {
  data: AccessOverview;
  fresh: string | null;
  onRemoved: (entry: AccessEntry) => void;
  onUncertain: () => void;
}) {
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);
  const entries = data.entries;
  if (entries.length === 0) {
    return (
      <div class="empty">
        No one is on the list yet.
        <div class="access-empty-sub">Look up a GitHub username above to let them sign up.</div>
      </div>
    );
  }
  const q = query.trim().toLowerCase().replace(/^@/, '');
  const shown = q ? entries.filter((e) => [e.login, e.githubId, e.note, e.addedBy]
    .some((v) => v?.toLowerCase().includes(q))) : entries;
  const pages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const cur = Math.min(page, pages - 1);
  const rows = shown.slice(cur * PAGE_SIZE, (cur + 1) * PAGE_SIZE);
  return (
    <section aria-label="People who can sign up">
      <div class="access-head">
        <span>{entries.length} {entries.length === 1 ? 'person' : 'people'}</span>
        {entries.length > SEARCH_FROM && (
          <input
            class="search access-search"
            type="search"
            placeholder="Search…"
            aria-label="Search people"
            value={query}
            onInput={(e) => { setQuery((e.target as HTMLInputElement).value); setPage(0); }}
          />
        )}
      </div>
      {entries.filter((e) => e.source === 'console').length >= data.limit && (
        <div class="banner warn" role="status">The list is full ({data.limit}). Remove someone to add another person.</div>
      )}
      {rows.length === 0 ? (
        <div class="empty">No one matches “{query.trim()}”.</div>
      ) : (
        <ul class="list" aria-label="People who can sign up">
          {rows.map((e) => (
            <AccessRow key={e.githubId} entry={e} fresh={fresh === e.githubId} onRemoved={onRemoved} onUncertain={onUncertain} />
          ))}
        </ul>
      )}
      {pages > 1 && (
        <div class="pager">
          <span class="pager-info">{cur * PAGE_SIZE + 1}–{Math.min((cur + 1) * PAGE_SIZE, shown.length)} of {shown.length}</span>
          <div class="pager-btns">
            <button class="btn ghost" disabled={cur === 0} onClick={() => setPage(cur - 1)}>← Prev</button>
            <span class="pager-pos">Page {cur + 1} / {pages}</span>
            <button class="btn ghost" disabled={cur >= pages - 1} onClick={() => setPage(cur + 1)}>Next →</button>
          </div>
        </div>
      )}
    </section>
  );
}

function AccessRow({ entry, fresh, onRemoved, onUncertain }: {
  entry: AccessEntry;
  fresh: boolean;
  onRemoved: (entry: AccessEntry) => void;
  onUncertain: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const name = label(entry);

  async function remove() {
    if (removing) return;
    setRemoving(true);
    setProblem(null);
    try {
      await removeAccount(entry.githubId);
      onRemoved(entry);
    } catch (e) {
      const p = accessProblem(e);
      if (p.code === 'request_timeout') {
        p.error += ' Refreshing the list to check.';
        onUncertain();
      }
      setProblem(p);
      setRemoving(false);
    }
  }

  const meta = entry.source === 'helm'
    ? `${entry.login ? `ID ${entry.githubId} · ` : ''}Set in the Helm values — change it there`
    : [`ID ${entry.githubId}`, entry.addedBy && `added by ${entry.addedBy}`, fmtAgo(entry.addedAt)]
      .filter(Boolean).join(' · ');
  return (
    <li class={`access-row${fresh ? ' fresh' : ''}`}>
      <div class="access-main">
        <Avatar id={entry.githubId} login={entry.login} />
        <div class="access-text">
          <div class="access-name">
            {entry.login ? <a href={`${GITHUB_WEB}/${entry.login}`} target="_blank" rel="noreferrer">{name}</a> : name}
            {entry.source === 'helm' && <span class="pill pill-helm" title="Set in the Helm values">Helm</span>}
          </div>
          <div class="row-meta" title={entry.addedAt ? new Date(entry.addedAt * 1000).toLocaleString() : undefined}>{meta}</div>
          {entry.note && <div class="access-note">{entry.note}</div>}
        </div>
        <WorkspaceLink workspace={entry.workspace} />
        {entry.source === 'console' && !confirming && (
          <button class="btn ghost access-remove" aria-label={`Remove ${name}`} onClick={() => setConfirming(true)}>
            Remove
          </button>
        )}
      </div>
      {confirming && (
        <div class="access-confirm" role="group" aria-label={`Confirm removing ${name}`}>
          <p>
            Remove {name}? They won’t be able to create a new workspace.
            {entry.workspace && ' Their existing workspace keeps working — delete it from its page if access must end.'}
          </p>
          <div class="access-actions">
            <button class="btn stop" disabled={removing} onClick={() => void remove()}>
              {removing ? 'Removing…' : 'Remove'}
            </button>
            <button class="btn ghost" disabled={removing} onClick={() => { setConfirming(false); setProblem(null); }}>
              Cancel
            </button>
          </div>
          {problem && <InlineProblem problem={problem} retry={remove} />}
        </div>
      )}
    </li>
  );
}

function WorkspaceLink({ workspace, quiet }: { workspace: AccessWorkspace | null; quiet?: boolean }) {
  if (!workspace) return quiet ? null : <span class="access-none">No workspace yet</span>;
  if (workspace.stage === 'ready') {
    return <a class="pill pill-running access-ws" href={`#/w/${workspace.slug}`}>Workspace →</a>;
  }
  const failed = workspace.stage === 'failed';
  return (
    <a class={`pill ${failed ? 'pill-degraded' : 'pill-transitioning'} access-ws`} href={`#/provision/${workspace.slug}`}>
      {failed ? 'Setup failed' : 'Setting up'}
    </a>
  );
}

function InlineProblem({ problem, retry }: { problem: Problem; retry: () => void }) {
  return (
    <div class="access-problem" role="alert">
      <span>{problem.error}</span>
      {problem.retryable && <button class="chip" onClick={() => void retry()}>Retry</button>}
      {problem.code === 'unauthorized' && <a class="panel-link" href="/oauth2/start">Sign in</a>}
      {/* A reference only helps with a fault on our side, not with a typo. */}
      {problem.retryable && problem.error_id && <span class="access-ref">Ref {problem.error_id}</span>}
    </div>
  );
}

function Avatar({ id, login, src }: { id: string; login: string | null; src?: string }) {
  const [failed, setFailed] = useState(false);
  // The initial sits under the photo, so the circle is never blank while the
  // photo loads (slow networks) and stays meaningful if it fails.
  return (
    <span class="access-avatar access-initial" aria-hidden="true">
      {(login || '#')[0].toUpperCase()}
      {!failed && (
        <img
          src={src || `${GITHUB_AVATARS}/u/${id}?s=80`}
          alt=""
          width={36}
          height={36}
          onError={() => setFailed(true)}
        />
      )}
    </span>
  );
}

function CopyLink({ url }: { url: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setState('copied');
    } catch {
      setState('failed');   // e.g. no clipboard permission: show the link to copy by hand
    }
  }
  if (state === 'failed') return <code class="access-url">{url}</code>;
  return <button class="btn ghost" onClick={() => void copy()}>{state === 'copied' ? 'Copied' : 'Copy signup link'}</button>;
}

function label(entry: AccessEntry): string {
  return entry.login ? `@${entry.login}` : `GitHub ID ${entry.githubId}`;
}

function hostOf(url: string | undefined): string {
  try {
    return url ? new URL(url).host : '';
  } catch {
    return '';
  }
}
