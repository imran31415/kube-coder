import { useEffect, useState } from 'preact/hooks';
import {
  listWorktrees,
  removeWorktree,
  sweepWorktrees,
  type WorktreeList,
  type WorktreeRow,
} from '../../api/worktrees';
import { ApiError } from '../../api/client';
import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { MutatorOnly } from '../../components/MutatorOnly';
import { pushToast } from '../../store/ui';
import { navigate, routeHref } from '../../store/router';
import { KEEP_REASON_LABEL, diffStatLabel, worktreeStateOf } from '../../util/worktree';

const STATE_PILL = {
  live: { tone: 'success', label: 'running' },
  finished: { tone: 'neutral', label: 'finished' },
  orphaned: { tone: 'warn', label: 'no Build' },
  manual: { tone: 'info', label: 'made by hand' },
} as const;

/**
 * Settings → Worktrees (#701): every isolated worktree on the workspace disk,
 * who owns it, why the cleanup kept it, and a Remove for the ones nobody
 * needs. The cleanup itself runs on its own every 10 minutes and only ever
 * takes what holds nothing — this is for the rest.
 */
export function WorktreesSection() {
  const [data, setData] = useState<WorktreeList | null>(null);
  const [target, setTarget] = useState<{ row: WorktreeRow; force: boolean } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function refresh() {
    try {
      setData(await listWorktrees());
    } catch {
      // Older server, or none reachable — the section still explains itself.
    }
  }
  useEffect(() => { void refresh(); }, []);

  async function onRemove(row: WorktreeRow, force: boolean) {
    setTarget(null);
    setBusy(row.path);
    try {
      const out = await removeWorktree(row.repo, row.slug, force);
      pushToast(`Removed ${row.slug}. Branch ${out.branch ?? row.branch} is kept.`, { kind: 'success' });
      await refresh();
    } catch (err) {
      const code = err instanceof ApiError ? (err.body as { code?: string } | null)?.code : undefined;
      if (code === 'dirty' && !force) {
        setTarget({ row, force: true });
      } else {
        pushToast(err instanceof ApiError ? err.message : String(err), { kind: 'danger' });
      }
    } finally {
      setBusy(null);
    }
  }

  async function onSweep() {
    setBusy('sweep');
    try {
      const r = await sweepWorktrees();
      pushToast(
        r.removed.length
          ? `Cleaned up ${r.removed.length} worktree${r.removed.length === 1 ? '' : 's'}.`
          : 'Nothing to clean up — every worktree still holds something.',
        { kind: r.removed.length ? 'success' : 'info' },
      );
      await refresh();
    } catch (err) {
      pushToast(err instanceof ApiError ? err.message : String(err), { kind: 'danger' });
    } finally {
      setBusy(null);
    }
  }

  const rows = data?.worktrees ?? [];
  return (
    <section class="settings-section">
      <h2 class="settings-section-title">
        Worktrees
        {data && (
          <Pill tone={data.count >= data.max ? 'warn' : 'neutral'} mono title="In use of KC_MAX_WORKTREES">
            {data.count} / {data.max}
          </Pill>
        )}
      </h2>
      <p class="settings-row-hint muted">
        Builds started with <strong>Isolated worktree</strong> each get their own folder, branch and
        port under <span class="mono">{data?.root ?? '~/.worktrees'}</span>. Finished ones that changed
        nothing, or whose work is already pushed, are cleaned up automatically; anything else stays
        until you remove it. Removing a worktree never deletes its branch.{' '}
        <a
          href={routeHref('/docs/builds-worktrees')}
          onClick={(e) => { e.preventDefault(); navigate('/docs/builds-worktrees'); }}
        >
          How isolation works
        </a>
      </p>

      {data && !data.available && (
        <p class="settings-row-hint muted">Worktree isolation is not available in this workspace.</p>
      )}
      {data?.available && rows.length === 0 && (
        <p class="settings-row-hint muted">No worktrees yet.</p>
      )}

      {rows.length > 0 && (
        <div class="settings-subs">
          {rows.map((row) => {
            const state = STATE_PILL[worktreeStateOf(row)];
            return (
              <div class="settings-sub-row" key={row.path}>
                <div class="settings-sub-label">
                  <span class="settings-sub-name mono">{row.branch}</span>
                  <Pill tone={state.tone} mono>{state.label}</Pill>
                  {row.stat && row.stat.files_changed > 0 && (
                    <span class="muted">{diffStatLabel(row.stat)}</span>
                  )}
                </div>
                <div class="settings-sub-control">
                  <MutatorOnly>
                    <Button
                      variant="secondary"
                      type="button"
                      disabled={busy === row.path || row.live}
                      title={row.live ? 'Stop its Build first' : 'Delete the folder — the branch is kept'}
                      onClick={() => setTarget({ row, force: false })}
                    >
                      <Icon name="trash" size={14} /> Remove
                    </Button>
                  </MutatorOnly>
                </div>
                <div class="settings-sub-note settings-radio-hint muted">
                  <span class="mono">{row.path}</span>
                  {row.port ? <> · port <span class="mono">{row.port}</span></> : null}
                  {row.task_id && (
                    <>
                      {' · '}
                      <a
                        href={routeHref(`/tasks/${encodeURIComponent(row.task_id)}/changes`)}
                        onClick={(e) => {
                          e.preventDefault();
                          navigate(`/tasks/${encodeURIComponent(row.task_id)}/changes`);
                        }}
                      >
                        {row.owner_name || row.task_id}
                      </a>
                    </>
                  )}
                  {row.keep_reason && KEEP_REASON_LABEL[row.keep_reason] && (
                    <> · kept: {KEEP_REASON_LABEL[row.keep_reason]}</>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {data?.available && (
        <MutatorOnly>
          <div class="settings-row">
            <div class="settings-row-label">
              Clean up now
              <div class="settings-radio-hint muted">
                Runs the automatic cleanup immediately. It only removes worktrees whose Build has
                finished and that hold nothing unique.
                {data.sweep.last_run_at && (
                  <> Last run {new Date(data.sweep.last_run_at * 1000).toLocaleString()}.</>
                )}
              </div>
            </div>
            <div class="settings-row-control">
              <Button type="button" disabled={busy === 'sweep'} onClick={() => void onSweep()}>
                {busy === 'sweep' ? 'Cleaning…' : 'Clean up now'}
              </Button>
            </div>
          </div>
        </MutatorOnly>
      )}

      <ConfirmDialog
        open={target !== null && !target.force}
        title="Remove this worktree?"
        body={target ? `The folder ${target.row.path} is deleted. The branch ${target.row.branch} and its commits are kept.` : ''}
        confirmLabel="Remove worktree"
        destructive
        onConfirm={() => target && void onRemove(target.row, false)}
        onCancel={() => setTarget(null)}
      />
      <ConfirmDialog
        open={target !== null && target.force}
        title="Discard uncommitted changes?"
        body={target ? `${target.row.path} has changes that were never committed. Removing it deletes them for good; committed work on ${target.row.branch} is kept.` : ''}
        confirmLabel="Discard and remove"
        destructive
        onConfirm={() => target && void onRemove(target.row, true)}
        onCancel={() => setTarget(null)}
      />
    </section>
  );
}
