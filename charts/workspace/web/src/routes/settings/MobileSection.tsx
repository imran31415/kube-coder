import { useEffect, useState } from 'preact/hooks';
import { getApiToken, regenerateApiToken } from '../../api/mobile';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Icon } from '../../components/Icon';
import { pushToast } from '../../store/ui';

/**
 * Self-serve mobile onboarding: shows the workspace host + Bearer API token so a
 * user can connect the kube-coder mobile app without shelling into the pod. The
 * token is fetched from the OAuth-gated /api/claude/auth/token (browser session
 * only), revealed on demand, copied with one tap, and rotatable.
 */
export function MobileSection() {
  // scheme+host with no path — exactly the host the mobile app's first screen wants.
  const host = window.location.origin;
  const [token, setToken] = useState<string | null>(null);
  const [revealed, setRevealed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  async function load() {
    setLoading(true);
    try {
      setToken((await getApiToken()).token);
    } catch {
      setToken(null); // surfaced as "Unavailable" below
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);

  async function copy(text: string, label: string) {
    try {
      await navigator.clipboard.writeText(text);
      pushToast(`${label} copied`, { kind: 'success' });
    } catch {
      pushToast(`Couldn't copy ${label.toLowerCase()} — select it and copy manually`, { kind: 'warn' });
    }
  }

  async function onRegenerate() {
    if (
      !window.confirm(
        'Regenerate the API token? Any device or script using the current token ' +
          'will stop working until you reconnect it with the new one.',
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      setToken((await regenerateApiToken()).token);
      setRevealed(true);
      pushToast('Token regenerated — reconnect your devices', { kind: 'success' });
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Regenerate failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  const tokenField = loading
    ? 'Loading…'
    : token
      ? revealed
        ? token
        : '•'.repeat(Math.min(token.length, 40))
      : 'Unavailable';

  return (
    <section class="settings-section">
      <h2 class="settings-section-title">Mobile app</h2>
      <p class="settings-row-hint muted">
        Drive this workspace from the kube-coder mobile app (iOS / Android) — list and
        message build sessions, browse memory, watch metrics. On the app's first screen,
        enter the host and token below, then tap <strong>Connect</strong>.
      </p>

      <div class="settings-row">
        <div class="settings-row-label">Workspace host</div>
        <div class="settings-row-control settings-copy-row">
          <Input fullWidth readOnly value={host} />
          <Button onClick={() => copy(host, 'Host')}>
            <Icon name="link" size={14} /> Copy
          </Button>
        </div>
      </div>

      <div class="settings-row">
        <div class="settings-row-label">API token</div>
        <div class="settings-row-control settings-copy-row">
          <Input fullWidth readOnly value={tokenField} />
          <Button onClick={() => setRevealed((v) => !v)} disabled={!token}>
            {revealed ? 'Hide' : 'Reveal'}
          </Button>
          <Button onClick={() => token && copy(token, 'Token')} disabled={!token}>
            <Icon name="link" size={14} /> Copy
          </Button>
        </div>
      </div>

      <p class="settings-row-hint muted">
        Keep this token secret — anyone with it can drive this workspace. The app stores
        it only on your device. Lost it or shared it by mistake? Rotate it:
      </p>
      <div class="settings-row">
        <div class="settings-row-label">Regenerate</div>
        <div class="settings-row-control">
          <Button variant="danger" onClick={onRegenerate} disabled={busy || !token}>
            {busy ? 'Regenerating…' : 'Regenerate token'}
          </Button>
        </div>
      </div>
    </section>
  );
}
