import { useEffect } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { MutatorOnly } from '../../components/MutatorOnly';
import { serverMode } from '../../store/server-mode';
import {
  devcontainerFor,
  loadDevcontainer,
  openApplyDialog,
  applyDialogWorkdir,
} from '../../store/devcontainer';
import { HOOKS, type HookName, type LifecycleEntry } from '../../api/devcontainer';
import { DevcontainerApplyDialog } from './DevcontainerApplyDialog';

/**
 * The Dev container card in the project brief (#594).
 *
 * Renders under Git, in the same shape as the Git section. Three jobs:
 *   1. Say what the repo declares (name, ports, extensions, hooks).
 *   2. Say what has actually been done, per hook.
 *   3. Say — prominently — what CANNOT be done here and why. That third one is
 *      the whole point: a `features` block that is silently ignored produces a
 *      workspace that looks configured and isn't.
 *
 * Renders nothing when the workdir has no devcontainer.json, so a project
 * without one is unchanged.
 */

const STATUS_LABEL: Record<string, string> = {
  pending: 'not run',
  running: 'running',
  done: 'ran',
  stale: 'changed — re-run',
  failed: 'failed',
  timed_out: 'timed out',
};

function relTime(epochSeconds?: number | null): string {
  if (!epochSeconds) return '';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function HookRow({ hook, entry }: { hook: HookName; entry: LifecycleEntry }) {
  const when = relTime(entry.ran_at);
  const failed = entry.status === 'failed' || entry.status === 'timed_out';
  return (
    <li class="cto-brief-item dc-hook">
      <span class={`dc-pill dc-pill-${entry.status}`}>
        {STATUS_LABEL[entry.status] ?? entry.status}
      </span>
      <code class="dc-hook-name">{hook}</code>
      {when && <span class="cto-brief-date">{when}</span>}
      {failed && typeof entry.exit_code === 'number' && entry.exit_code >= 0 && (
        <span class="dc-exit">exit {entry.exit_code}</span>
      )}
    </li>
  );
}

export function DevcontainerPanel({ workdir }: { workdir: string }) {
  useEffect(() => {
    if (workdir) void loadDevcontainer(workdir);
  }, [workdir]);

  const rec = devcontainerFor(workdir);
  // No file here, or the feature is off — render nothing rather than an empty
  // card. Most projects have no devcontainer and should look untouched.
  if (!rec || !rec.found) return null;

  if (rec.error) {
    return (
      <section class="cto-brief-section">
        <h3 class="cto-brief-h">Dev container</h3>
        <p class="dc-error">
          <code>{rec.rel_path}</code> could not be read: {rec.error}
        </p>
      </section>
    );
  }

  const blocking = rec.unsupported.filter((u) => u.severity === 'blocking');
  const other = rec.unsupported.filter((u) => u.severity !== 'blocking');
  const facts: string[] = [];
  if (rec.ports.length) facts.push(`${rec.ports.length} ports`);
  if (rec.extensions.length) facts.push(`${rec.extensions.length} extensions`);
  const settingsCount = Object.keys(rec.settings).length;
  if (settingsCount) facts.push(`${settingsCount} settings`);
  const envCount = Object.keys(rec.env).length;
  if (envCount) facts.push(`${envCount} env`);

  const hookRows = HOOKS
    .map((h) => [h, rec.lifecycle_status[h]] as const)
    .filter(([, e]) => e && e.status !== 'none');

  return (
    <section class="cto-brief-section dc-section">
      <h3 class="cto-brief-h">Dev container</h3>

      <p class="dc-head">
        <Icon name="mission" size={12} />
        <span class="dc-name">{rec.name || rec.rel_path}</span>
        {facts.length > 0 && <span class="dc-facts">{facts.join(' · ')}</span>}
      </p>
      <p class="dc-path"><code>{rec.rel_path}</code></p>

      {hookRows.length > 0 && (
        <ul class="cto-brief-list dc-hooks">
          {hookRows.map(([hook, entry]) => (
            <HookRow key={hook} hook={hook} entry={entry as LifecycleEntry} />
          ))}
        </ul>
      )}

      {blocking.length > 0 && (
        // Expanded, not collapsed. Someone who never opens this block would
        // otherwise believe their `features` were installed.
        <div class="dc-unsupported dc-unsupported-blocking">
          <p class="dc-unsupported-head">
            ⚠ {blocking.length} {blocking.length === 1 ? 'property' : 'properties'}{' '}
            can&apos;t be applied here
          </p>
          <ul class="dc-unsupported-list">
            {blocking.map((u) => (
              <li key={u.key}>
                <code>{u.key}</code>
                <span class="dc-reason">{u.reason}</span>
                {u.remedy && <span class="dc-remedy">{u.remedy}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {other.length > 0 && (
        <details class="dc-unsupported">
          <summary>{other.length} more not applied here</summary>
          <ul class="dc-unsupported-list">
            {other.map((u) => (
              <li key={u.key}>
                <code>{u.key}</code>
                <span class="dc-reason">{u.reason}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {rec.caveats.length > 0 && (
        <ul class="dc-caveats">
          {rec.caveats.map((c) => <li key={c}>{c}</li>)}
        </ul>
      )}

      <MutatorOnly>
        <button
          type="button"
          class="dc-apply-btn"
          disabled={rec.busy}
          onClick={() => openApplyDialog(workdir)}
        >
          {rec.busy ? 'Running…' : 'Apply…'}
        </button>
      </MutatorOnly>
      {serverMode.value.readOnly && (
        <p class="dc-readonly">Read-only workspace — nothing can be applied.</p>
      )}

      {applyDialogWorkdir.value === workdir && (
        <DevcontainerApplyDialog workdir={workdir} />
      )}
    </section>
  );
}
