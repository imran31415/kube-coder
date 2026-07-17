import { useEffect, useState } from 'preact/hooks';
import {
  getGithubFullStatus,
  generateSshKey,
  setGitConfig,
  setAuthMode,
  type GithubFullStatus,
  type GitAuthMode,
} from '../../api/github';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { GithubConnect } from '../../components/GithubConnect';
import { pushToast } from '../../store/ui';

const MODES: { id: GitAuthMode; label: string }[] = [
  { id: 'app', label: 'App (managed)' },
  { id: 'personal', label: 'Personal' },
];

export function GitSection() {
  const [status, setStatus] = useState<GithubFullStatus | null>(null);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [modeBusy, setModeBusy] = useState(false);

  function applyStatus(s: GithubFullStatus) {
    setStatus(s);
    setName(s.git_config?.user_name ?? '');
    setEmail(s.git_config?.user_email ?? '');
  }

  async function refresh() {
    try {
      applyStatus(await getGithubFullStatus());
    } catch {
      // server unavailable / unauthorized — leave status null
    }
  }
  useEffect(() => { void refresh(); }, []);

  const mode: GitAuthMode = status?.auth_mode ?? 'app';
  const appAvailable = status?.app_available ?? true;
  const ghUser = status?.gh_cli?.username?.trim();

  async function onSetMode(next: GitAuthMode) {
    if (next === mode || modeBusy) return;
    setModeBusy(true);
    try {
      applyStatus(await setAuthMode(next));
      if (next === 'personal') {
        pushToast('Personal mode on. If git/gh still use the bot, run `gh auth login` in a terminal.', { kind: 'info' });
      } else {
        pushToast('Using the managed GitHub App token.', { kind: 'success' });
      }
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Could not switch mode', { kind: 'danger' });
    } finally {
      setModeBusy(false);
    }
  }

  async function onSaveConfig(e: Event) {
    e.preventDefault();
    if (!name.trim() || !email.trim()) return;
    setBusy(true);
    try {
      await setGitConfig(name.trim(), email.trim());
      pushToast('Git identity saved', { kind: 'success' });
      await refresh();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Save failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  async function onGenKey() {
    if (!email.trim()) {
      pushToast('Set your git email first', { kind: 'warn' });
      return;
    }
    setBusy(true);
    try {
      await generateSshKey(email.trim());
      pushToast('SSH key generated. Add the public key to GitHub.', { kind: 'success' });
      await refresh();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'ssh-keygen failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section class="settings-section">
      <h2 class="settings-section-title">GitHub &amp; SSH</h2>

      <div class="settings-row">
        <div class="settings-row-label">Connect account</div>
        <div class="settings-row-control settings-row-control-stack">
          <GithubConnect
            connected={mode === 'personal' && !!ghUser}
            user={ghUser}
            onConnected={() => void refresh()}
          />
        </div>
      </div>

      <div class="settings-row">
        <div class="settings-row-label">Auth mode</div>
        <div class="settings-row-control settings-row-control-stack">
          <div class="seg">
            {MODES.map((m) => (
              <button
                key={m.id}
                class={`seg-item ${mode === m.id ? 'seg-item-active' : ''}`}
                disabled={modeBusy || (m.id === 'app' && !appAvailable)}
                onClick={() => void onSetMode(m.id)}
              >
                {m.label}
              </button>
            ))}
          </div>
          <div class="settings-row-hint muted">
            {mode === 'personal'
              ? `Using your own GitHub login${ghUser ? ` (${ghUser})` : ''}. Sign in with `
              : 'Using the workspace’s managed GitHub App token — no personal login needed.'}
            {mode === 'personal' && <code class="mono">gh auth login</code>}
            {mode === 'personal' && ' in a terminal if git/gh still act as the bot.'}
          </div>
        </div>
      </div>

      <p class="settings-row-hint muted">
        Status:
        {' '}
        <Pill tone={status?.ssh?.configured ? 'success' : 'warn'} mono>
          {status?.ssh?.configured ? 'SSH key ✓' : 'SSH key missing'}
        </Pill>
        {' '}
        <Pill tone={status?.gh_cli?.authenticated ? 'success' : 'warn'} mono>
          {status?.gh_cli?.authenticated ? `gh CLI ✓ ${ghUser ?? ''}`.trim() : 'gh CLI not signed in'}
        </Pill>
      </p>

      <form onSubmit={onSaveConfig}>
        <div class="settings-row">
          <div class="settings-row-label">git user.name</div>
          <div class="settings-row-control">
            <Input fullWidth value={name} onInput={(e) => setName((e.target as HTMLInputElement).value)} placeholder="Imran Hassanali" />
          </div>
        </div>
        <div class="settings-row">
          <div class="settings-row-label">git user.email</div>
          <div class="settings-row-control">
            <Input fullWidth value={email} onInput={(e) => setEmail((e.target as HTMLInputElement).value)} placeholder="you@example.com" type="email" />
          </div>
        </div>
        <div class="settings-row">
          <div class="settings-row-label" />
          <div class="settings-row-control" style={{ gap: 'var(--size-2)' }}>
            <Button variant="primary" type="submit" disabled={busy}>
              <Icon name="check" size={14} /> Save identity
            </Button>
            <Button variant="secondary" type="button" onClick={onGenKey} disabled={busy}>
              <Icon name="plus" size={14} /> Generate SSH key
            </Button>
          </div>
        </div>
      </form>

      {status?.ssh?.public_key && (
        <details class="settings-pubkey">
          <summary>Show SSH public key</summary>
          <pre class="mono">{status.ssh.public_key}</pre>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              navigator.clipboard?.writeText(status.ssh?.public_key ?? '');
              pushToast('Copied', { kind: 'info' });
            }}
          >
            Copy
          </Button>
        </details>
      )}
    </section>
  );
}
