import { useState } from 'preact/hooks';
import { ClaudeConnect } from './ClaudeConnect';
import { Button } from './primitives/Button';
import { Input } from './primitives/Input';
import { Icon } from './Icon';
import { setProviderKey } from '../api/providerKeys';
import { pushToast } from '../store/ui';
import './ClaudeCredentialSetup.css';

export interface ClaudeCredentialSetupProps {
  /** Tri-state credential presence from store/claude: true shows the connected
   *  confirmation, false/null shows the two connect options. */
  ready: boolean | null;
  /** Fired after a successful connect via EITHER path (OAuth or API key) so the
   *  parent can re-probe readiness (refreshClaudeReady) and advance. */
  onConnected?: () => void;
}

/**
 * The shared "connect your Claude account" panel (#494). Offers BOTH options
 * the founder locked in, OAuth-first: browser sign-in with a Claude Pro/Max
 * subscription (recommended — no key to hunt for), and an ANTHROPIC_API_KEY
 * paste as the secondary path for users who already have a key. Reused by the
 * onboarding Claude step and the AI CTO first-win gate so both surfaces present
 * the same choice; the underlying flows (ClaudeConnect, ProviderKeysSection)
 * are untouched.
 */
export function ClaudeCredentialSetup({ ready, onConnected }: ClaudeCredentialSetupProps) {
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);

  async function saveKey() {
    const trimmed = key.trim();
    if (!trimmed) return;
    setBusy(true);
    try {
      await setProviderKey('ANTHROPIC_API_KEY', trimmed);
      setKey('');
      pushToast('Anthropic API key saved', { kind: 'success' });
      onConnected?.();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Save failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  if (ready) {
    return (
      <div class="ccs ccs-done">
        <p class="ghc-connected">
          <Icon name="check" size={14} /> Claude is connected. You're ready to build.
        </p>
      </div>
    );
  }

  return (
    <div class="ccs">
      <div class="ccs-option ccs-option-primary">
        <div class="ccs-option-head">
          <span class="ccs-option-title">Sign in with your Claude account</span>
          <span class="ccs-badge">Recommended</span>
        </div>
        <ClaudeConnect onConnected={() => onConnected?.()} />
      </div>

      <div class="ccs-or" aria-hidden>
        <span>or</span>
      </div>

      <div class="ccs-option">
        <div class="ccs-option-head">
          <span class="ccs-option-title">Paste an API key</span>
        </div>
        <p class="ccs-hint muted">
          Already have an <code>ANTHROPIC_API_KEY</code>? Paste it — billed to your Anthropic API account.
        </p>
        <div class="ccs-key-row">
          <Input
            fullWidth
            type="password"
            aria-label="Anthropic API key"
            value={key}
            placeholder="sk-ant-…"
            disabled={busy}
            onInput={(e) => setKey((e.target as HTMLInputElement).value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void saveKey(); }}
          />
          <Button variant="secondary" type="button" disabled={busy || !key.trim()} onClick={() => void saveKey()}>
            <Icon name="check" size={14} /> Save key
          </Button>
        </div>
      </div>
    </div>
  );
}
