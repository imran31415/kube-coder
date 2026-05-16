import { useEffect, useState } from 'preact/hooks';
import { githubStatus, generateSshKey, setGitConfig, type GitHubStatus } from '../../api/github';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { pushToast } from '../../store/ui';

export function GitSection() {
  const [status, setStatus] = useState<GitHubStatus | null>(null);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      const s = await githubStatus();
      setStatus(s);
      setName(s.git_user_name ?? '');
      setEmail(s.git_user_email ?? '');
    } catch {
      // server unavailable — leave status null
    }
  }
  useEffect(() => { void refresh(); }, []);

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
      <p class="settings-row-hint muted">
        Status:
        {' '}
        <Pill tone={status?.ssh_key_exists ? 'success' : 'warn'} mono>
          {status?.ssh_key_exists ? 'SSH key ✓' : 'SSH key missing'}
        </Pill>
        {' '}
        <Pill tone={status?.gh_authenticated ? 'success' : 'warn'} mono>
          {status?.gh_authenticated ? `gh CLI ✓ ${status?.gh_user ?? ''}`.trim() : 'gh CLI not signed in'}
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

      {status?.ssh_public_key && (
        <details class="settings-pubkey">
          <summary>Show SSH public key</summary>
          <pre class="mono">{status.ssh_public_key}</pre>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              navigator.clipboard?.writeText(status.ssh_public_key ?? '');
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
