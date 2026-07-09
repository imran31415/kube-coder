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
  const [restarting, setRestarting] = useState(false);

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

  // Once the update has actually rolled the pod, keep this tab on a full-screen
  // "restarting…" overlay and poll /health until the freshly-recreated pod is
  // back, then reload into it. This bridges the ~1 min Recreate downtime: the
  // already-loaded SPA keeps running here, and if the user reloads mid-window
  // the ingress serves the matching holding page (issue #184). We only reload
  // after seeing the pod go DOWN and then come back UP, so we never bounce into
  // the still-terminating old pod.
  useEffect(() => {
    if (!restarting) return;
    let cancelled = false;
    let timer = 0;
    let sawDown = false;
    const poll = async () => {
      try {
        const r = await fetch('/health', { cache: 'no-store', redirect: 'manual' });
        if (cancelled) return;
        if (r.ok) {
          if (sawDown) { window.location.reload(); return; }
        } else {
          sawDown = true;
        }
      } catch {
        // Pod down / network blip during the rollout — treat as "down".
        sawDown = true;
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 4000);
    };
    // Give the old pod a moment to begin terminating before the first probe.
    timer = window.setTimeout(() => void poll(), 6000);
    // Safety net: if we somehow never observe recovery, reload anyway so the
    // user isn't stranded on the overlay forever.
    const fallback = window.setTimeout(() => {
      if (!cancelled) window.location.reload();
    }, 5 * 60 * 1000);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.clearTimeout(fallback);
    };
  }, [restarting]);

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
      if (r.rolled) {
        // The pod is being Recreate-restarted — hand off to the full-screen
        // restarting overlay, which polls its way back to the live workspace.
        setRestarting(true);
      } else {
        // No-op update (already on target): nothing restarted, just refresh.
        pushToast(`Already up to date (${r.toVersion ?? current}).`, { kind: 'success' });
        window.setTimeout(() => void refresh(), 1000);
      }
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Update failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  if (restarting) {
    return (
      <div class="ws-restarting-overlay" role="status" aria-live="polite">
        <div class="ws-restarting-card">
          <div class="ws-restarting-spinner" aria-hidden="true" />
          <h1 class="ws-restarting-title">Your workspace is restarting…</h1>
          <p class="ws-restarting-text">
            Applying the update and bringing your workspace back online.
          </p>
          <p class="ws-restarting-eta">
            This usually takes about a minute — this page returns automatically
            when it's ready.
          </p>
        </div>
      </div>
    );
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
