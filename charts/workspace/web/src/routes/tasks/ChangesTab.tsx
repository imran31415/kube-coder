import { useEffect, useState } from 'preact/hooks';
import {
  getTaskWorktree,
  getTaskWorktreeDiff,
  removeTaskWorktree,
  type TaskWorktreeView,
  type WorktreeDiff,
  type WorktreeFile,
} from '../../api/tasks';
import { ApiError } from '../../api/client';
import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { MutatorOnly } from '../../components/MutatorOnly';
import { Icon } from '../../components/Icon';
import { pushToast } from '../../store/ui';
import { diffStatLabel, sha7 } from '../../util/worktree';
import './changes.css';

/** How often the tab refreshes while the Build is still working. */
const LIVE_POLL_MS = 10_000;

const STATUS_WORD: Record<string, string> = {
  A: 'added', M: 'modified', D: 'deleted', '?': 'new, untracked', T: 'type changed',
};

const REMOVE_HINT: Record<string, string> = {
  live: 'Stop the Build before removing its worktree.',
  removed: 'This worktree has already been removed.',
  repo_missing: 'The repository this worktree came from is gone.',
};

/**
 * The Changes tab of an isolated Build (#701): which branch it works on, what
 * it changed since it started (committed or not), each file's diff, how to
 * get the work out, and how to remove the folder when done.
 *
 * Shown for finished Builds too — a finished Build is exactly when someone
 * reviews what it did.
 */
export function ChangesTab({ taskId, live }: { taskId: string; live: boolean }) {
  const [view, setView] = useState<TaskWorktreeView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [diff, setDiff] = useState<WorktreeDiff | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<'remove' | 'force' | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  async function load(fresh = false) {
    try {
      setView(await getTaskWorktree(taskId, fresh));
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : String(err));
    }
  }

  useEffect(() => {
    setView(null);
    setOpenFile(null);
    setDiff(null);
    void load();
    if (!live) return undefined;
    const id = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void load();
    }, LIVE_POLL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, live]);

  async function toggleFile(f: WorktreeFile) {
    if (openFile === f.path) {
      setOpenFile(null);
      return;
    }
    setOpenFile(f.path);
    setDiff(null);
    setDiffError(null);
    try {
      setDiff(await getTaskWorktreeDiff(taskId, f.path));
    } catch (err) {
      setDiffError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function copyPush() {
    if (!view?.push_command) return;
    try {
      await navigator.clipboard.writeText(view.push_command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      pushToast('Clipboard unavailable — select the command and copy it.', { kind: 'warn' });
    }
  }

  async function remove(force: boolean) {
    setConfirm(null);
    setBusy(true);
    try {
      const out = await removeTaskWorktree(taskId, force);
      pushToast(
        `Worktree removed. Branch ${out.branch ?? view?.worktree.branch ?? ''} is kept.`,
        { kind: 'success' },
      );
      await load(true);
    } catch (err) {
      const code = err instanceof ApiError
        ? (err.body as { code?: string } | null)?.code
        : undefined;
      if (code === 'dirty' && !force) {
        setConfirm('force');          // ask again, with the cost spelled out
      } else {
        pushToast(err instanceof ApiError ? err.message : String(err), { kind: 'danger' });
      }
    } finally {
      setBusy(false);
    }
  }

  if (loadError && !view) {
    return (
      <div class="wt-tab">
        <p class="wt-error" role="alert">{loadError}</p>
        <Button size="sm" onClick={() => void load(true)}>Try again</Button>
      </div>
    );
  }
  if (!view) return <div class="wt-tab muted">Loading changes…</div>;

  const w = view.worktree;
  const st = view.status;
  const blocked = view.remove_blocked;
  const uncommitted = st ? st.dirty + st.untracked : 0;

  return (
    <div class="wt-tab">
      <header class="wt-head">
        <div class="wt-branch">
          <span class="wt-branch-name mono" title={w.path}>⎇ {st?.branch || w.branch}</span>
          <span class="muted">
            from {w.base_ref || 'HEAD'}
            {w.base_sha && <span class="mono"> @ {sha7(w.base_sha)}</span>}
          </span>
        </div>
        <div class="wt-pills">
          {typeof st?.ahead === 'number' && (
            <Pill tone={st.ahead ? 'accent' : 'neutral'} title="Commits on this branch since it started">
              {st.ahead} commit{st.ahead === 1 ? '' : 's'}
            </Pill>
          )}
          {typeof st?.behind === 'number' && st.behind > 0 && (
            <Pill tone="warn" title={`${w.base_ref} has moved on since this branch started`}>
              {st.behind} behind {w.base_ref}
            </Pill>
          )}
          {uncommitted > 0 && (
            <Pill tone="warn" title="Changes not committed yet">
              {uncommitted} uncommitted
            </Pill>
          )}
          {w.port && <Pill mono title="This Build's dev server port ($PORT)">:{w.port}</Pill>}
          {view.live && <Pill tone="success">running</Pill>}
        </div>
      </header>

      {!view.exists ? (
        <p class="wt-note" role="status">
          This worktree has been removed.{' '}
          {view.branch_exists
            ? <>Its branch <span class="mono">{w.branch}</span> and its commits are kept.</>
            : <>Its branch is gone too.</>}
        </p>
      ) : (
        <>
          {view.status_error && <p class="wt-error">{view.status_error}</p>}
          {st && (
            <section class="wt-files" aria-label="Changed files">
              <div class="wt-summary">
                <strong>{diffStatLabel(st)}</strong>
                {st.detached && <span class="muted"> · HEAD is detached</span>}
                <button
                  type="button"
                  class="wt-link"
                  onClick={() => void load(true)}
                  title="Re-read the worktree now"
                >
                  ↻ Refresh
                </button>
              </div>
              {st.files.length > 0 && (
                <ul class="wt-file-list">
                  {st.files.map((f) => (
                    <li key={f.path} class={`wt-file ${openFile === f.path ? 'wt-file-open' : ''}`}>
                      <button
                        type="button"
                        class="wt-file-row"
                        onClick={() => void toggleFile(f)}
                        aria-expanded={openFile === f.path}
                        title={STATUS_WORD[f.status] ?? f.status}
                      >
                        <span class={`wt-st wt-st-${f.status === '?' ? 'u' : f.status}`}>{f.status}</span>
                        <span class="wt-path mono">{f.path}</span>
                        {f.binary
                          ? <span class="muted">binary</span>
                          : typeof f.added === 'number' && (
                            <span class="wt-counts mono">
                              <span class="wt-add">+{f.added}</span> <span class="wt-del">−{f.deleted}</span>
                            </span>
                          )}
                        {f.uncommitted && <Pill tone="warn">uncommitted</Pill>}
                      </button>
                      {openFile === f.path && (
                        <div class="wt-diff-wrap">
                          {diffError && <p class="wt-error">{diffError}</p>}
                          {!diff && !diffError && <p class="muted">Loading diff…</p>}
                          {diff && <DiffView diff={diff} />}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {st.truncated && (
                <p class="muted">Showing the first {st.files.length} of {st.files_changed} files.</p>
              )}
            </section>
          )}
        </>
      )}

      <section class="wt-actions">
        {view.push_command && (
          <div class="wt-push">
            <span class="ntf-label">Get the work out</span>
            <div class="wt-push-row">
              <code class="wt-cmd mono">{view.push_command}</code>
              <Button size="sm" onClick={() => void copyPush()} aria-label="Copy push command">
                <Icon name="copy" size={12} /> {copied ? 'Copied' : 'Copy'}
              </Button>
            </div>
            <span class="muted">
              Then open a pull request from <span class="mono">{w.branch}</span>.
            </span>
          </div>
        )}
        {view.exists && (
          <MutatorOnly>
            <div class="wt-remove">
              <Button
                variant="danger"
                size="sm"
                disabled={busy || blocked === 'live' || blocked === 'removed'}
                onClick={() => setConfirm(blocked === 'dirty' ? 'force' : 'remove')}
                title={REMOVE_HINT[blocked] ?? 'Delete the folder — the branch and its commits are kept'}
              >
                <Icon name="trash" size={12} /> Remove worktree
              </Button>
              {REMOVE_HINT[blocked] && <span class="muted">{REMOVE_HINT[blocked]}</span>}
            </div>
          </MutatorOnly>
        )}
      </section>

      <ConfirmDialog
        open={confirm === 'remove'}
        title="Remove this worktree?"
        body={`The folder ${w.path} is deleted. The branch ${w.branch} and its commits are kept.`}
        confirmLabel="Remove worktree"
        destructive
        onConfirm={() => void remove(false)}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm === 'force'}
        title="Discard uncommitted changes?"
        body={`This worktree has ${uncommitted || 'some'} uncommitted change${uncommitted === 1 ? '' : 's'} that are not on any branch. Removing it deletes them for good. Committed work on ${w.branch} is kept.`}
        confirmLabel="Discard and remove"
        destructive
        onConfirm={() => void remove(true)}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}

/** A unified diff with added/removed/hunk lines tinted. Plain text — never
 *  rendered as markup, because the content is whatever the agent wrote. */
function DiffView({ diff }: { diff: WorktreeDiff }) {
  if (diff.binary) return <p class="muted">Binary file — no text diff.</p>;
  const lines = diff.diff.split('\n');
  return (
    <>
      <pre class="wt-diff mono" aria-label={`Diff of ${diff.file}`}>
        {lines.map((line, i) => (
          <span key={i} class={lineClass(line)}>{line}{'\n'}</span>
        ))}
      </pre>
      {diff.truncated && <p class="muted">Diff truncated — it is larger than the viewer shows.</p>}
    </>
  );
}

function lineClass(line: string): string {
  if (line.startsWith('@@')) return 'wt-l-hunk';
  if (line.startsWith('+++') || line.startsWith('---')) return 'wt-l-meta';
  if (line.startsWith('+')) return 'wt-l-add';
  if (line.startsWith('-')) return 'wt-l-del';
  return '';
}
