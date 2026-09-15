import { useState } from 'preact/hooks';
import { Modal } from '../../components/Modal';
import { Button } from '../../components/primitives/Button';
import {
  devcontainerFor,
  closeApplyDialog,
  applyDevcontainerTo,
  applyingDevcontainer,
} from '../../store/devcontainer';
import { HOOKS, type HookName } from '../../api/devcontainer';

/**
 * The consent dialog (#594).
 *
 * This is the only place in the dashboard where a user authorizes running code
 * that came out of a cloned repo, so it is deliberately blunt:
 *
 *   * Every command is rendered VERBATIM in a <pre>. No truncation, no
 *     summarizing — the user approves the exact text that will run.
 *   * Blocking unsupported properties are repeated ABOVE the command list, so
 *     nobody approves a postCreate believing their `features` already
 *     installed.
 *   * `needs_root` warnings sit inline with the command that carries them. The
 *     pod cannot escalate, so those commands WILL fail; saying so before the
 *     click turns a confusing 15-minute failure into an upfront decision.
 *   * The config hash loaded with the record is sent back with the request.
 *     If devcontainer.json changed in between, the server 409s rather than
 *     running text the user never saw.
 *
 * Hooks are opt-in per checkbox and default to OFF. The safe action — pin the
 * ports, write the settings, install the extensions — is one click away
 * without running anything.
 */
export function DevcontainerApplyDialog({ workdir }: { workdir: string }) {
  const rec = devcontainerFor(workdir);
  const [selected, setSelected] = useState<Set<HookName>>(new Set());
  const [autoApply, setAutoApply] = useState(false);

  if (!rec || !rec.found || rec.error) return null;

  const available = HOOKS.filter((h) => (rec.lifecycle[h] ?? []).length > 0);
  const blocking = rec.unsupported.filter((u) => u.severity === 'blocking');
  const anyRootNeeded = available.some((h) =>
    rec.lifecycle[h].some((c) => c.needs_root));

  const toggle = (hook: HookName) => {
    const next = new Set(selected);
    if (next.has(hook)) next.delete(hook);
    else next.add(hook);
    setSelected(next);
  };

  const onConfirm = async () => {
    const hooks = HOOKS.filter((h) => selected.has(h));
    const outcome = await applyDevcontainerTo(
      workdir, hooks, rec.config_hash, autoApply || undefined);
    // A conflict keeps the dialog open against the RELOADED record, so the
    // user re-reads what actually changed instead of blind-retrying.
    if (outcome.ok) closeApplyDialog();
  };

  return (
    <Modal open onClose={closeApplyDialog} labelledBy="dc-apply-title" width={620}>
      <h2 id="dc-apply-title" class="dc-dialog-title">
        Apply {rec.name || 'devcontainer.json'}
      </h2>
      <p class="dc-dialog-sub"><code>{rec.rel_path}</code></p>

      {blocking.length > 0 && (
        <div class="dc-unsupported dc-unsupported-blocking">
          <p class="dc-unsupported-head">
            These will NOT be applied, whatever you select below:
          </p>
          <ul class="dc-unsupported-list">
            {blocking.map((u) => (
              <li key={u.key}>
                <code>{u.key}</code>
                <span class="dc-reason">{u.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div class="dc-dialog-block">
        <p class="dc-dialog-h">Applied immediately</p>
        <ul class="dc-dialog-list">
          <li>{rec.ports.length} forwarded port(s) pinned on the Apps page</li>
          <li>{rec.extensions.length} extension(s) installed into code-server</li>
          <li>{Object.keys(rec.settings).length} editor setting(s) merged</li>
          <li>{Object.keys(rec.env).length} environment variable(s) for new builds</li>
        </ul>
      </div>

      {available.length > 0 && (
        <div class="dc-dialog-block">
          <p class="dc-dialog-h">Commands from this repo</p>
          <p class="dc-dialog-warn">
            These run in your workspace with your GitHub token and provider keys
            in reach. Nothing runs unless you tick it.
          </p>
          {available.map((hook) => (
            <div key={hook} class="dc-hook-block">
              <label class="dc-hook-label">
                <input
                  type="checkbox"
                  checked={selected.has(hook)}
                  onChange={() => toggle(hook)}
                />
                <code>{hook}</code>
                <span class="dc-hook-count">
                  {rec.lifecycle[hook].length} command(s)
                </span>
              </label>
              {rec.lifecycle[hook].map((cmd, i) => (
                <div key={`${hook}-${i}`} class="dc-cmd">
                  {cmd.name && <span class="dc-cmd-name">{cmd.name}</span>}
                  <pre class="dc-cmd-text">{cmd.display}</pre>
                  {cmd.needs_root && (
                    <p class="dc-cmd-root">
                      ⚠ needs root — {cmd.root_reasons.join('; ')}. This
                      workspace runs as UID 1000 with no privilege escalation,
                      so this will fail.
                    </p>
                  )}
                  {cmd.caveats.map((c) => (
                    <p key={c} class="dc-cmd-caveat">{c}</p>
                  ))}
                </div>
              ))}
            </div>
          ))}
          {anyRootNeeded && (
            <p class="dc-dialog-warn">
              Install into <code>$HOME</code> instead (nvm, pyenv, rustup,
              <code>pip --user</code>) — those persist on the workspace volume.
            </p>
          )}
          {selected.has('postStart') && (
            <label class="dc-hook-label dc-auto">
              <input
                type="checkbox"
                checked={autoApply}
                onChange={() => setAutoApply(!autoApply)}
              />
              Re-run <code>postStart</code> after every pod restart
              <span class="dc-hook-count">
                only while devcontainer.json is unchanged
              </span>
            </label>
          )}
        </div>
      )}

      <div class="dc-dialog-actions">
        <Button variant="secondary" onClick={closeApplyDialog}>Cancel</Button>
        <Button
          variant={selected.size ? 'danger' : 'primary'}
          class="dc-confirm"
          disabled={applyingDevcontainer.value}
          onClick={onConfirm}
        >
          {selected.size
            ? `Apply and run ${selected.size} hook(s)`
            : 'Apply without running commands'}
        </Button>
      </div>
    </Modal>
  );
}
