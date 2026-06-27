import { useEffect, useState } from 'preact/hooks';
import {
  getWorkspaceVersion,
  updateWorkspace,
  type WorkspaceVersion,
} from '../../api/update';
import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { pushToast } from '../../store/ui';

export function UpdatesSection() {
  const [info, setInfo] = useState<WorkspaceVersion | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      setInfo(await getWorkspaceVersion());
    } catch {
      // server unavailable — leave info null; the section stays hidden.
    } finally {
      setLoaded(true);
    }
  }
  useEffect(() => { void refresh(); }, []);

  // Self-serve disabled (or not yet loaded) => render nothing, so default
  // deployments don't show a dead section.
  if (!loaded || !info || !info.available) return null;

  const current = info.version ?? 'unknown';
  const latest = info.latestVersion ?? null;
  const canUpdate = !!info.updateAvailable;

  async function onUpdate() {
    if (
      !window.confirm(
        `Restart this workspace and pull ${latest ?? 'the latest release'}?\n\n` +
          `The pod restarts — running processes, terminal sessions and unsaved ` +
          `in-memory state are lost. Your /home/dev disk is preserved.`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      const r = await updateWorkspace();
      if (r.error) throw new Error(r.error);
      pushToast(
        `Updating ${r.fromVersion ?? '?'} → ${r.toVersion}. The pod is restarting…`,
        { kind: 'success' },
      );
      // The pod is rolling out; re-poll shortly so the badge clears once up.
      window.setTimeout(() => void refresh(), 3000);
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Update failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section class="settings-section">
      <h2 class="settings-section-title">Updates</h2>
      <p class="settings-row-hint muted">
        Workspace version:{' '}
        <Pill tone={canUpdate ? 'warn' : 'success'} mono>
          {current}
        </Pill>
        {canUpdate && latest && (
          <>
            {' → '}
            <Pill tone="success" mono>{latest}</Pill>
          </>
        )}
      </p>

      <div class="settings-row">
        <div class="settings-row-label">
          {canUpdate ? 'Update available' : 'Up to date'}
        </div>
        <div class="settings-row-control settings-row-control-stack">
          <div class="settings-row-hint muted">
            {canUpdate
              ? `A newer release (${latest}) is available. Updating restarts the pod onto the new image.`
              : latest
                ? `Running the latest release (${latest}).`
                : 'Latest-release lookup is currently unavailable.'}
          </div>
          <Button
            variant={canUpdate ? 'primary' : 'secondary'}
            disabled={busy || !canUpdate}
            onClick={() => void onUpdate()}
          >
            {busy ? 'Updating…' : canUpdate ? 'Restart & update' : 'Up to date'}
          </Button>
        </div>
      </div>
    </section>
  );
}
