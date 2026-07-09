import { useState } from 'preact/hooks';
import { getAdminToken } from '../api/admin';

/**
 * "Mobile access" card on the workspace list. Lets a signed-in admin reveal the
 * controller host + admin token to paste into the kube-coder mobile app. The
 * token is fetched only on an explicit click (not on mount) so it never sits in
 * the page unless asked for, and the endpoint is gated to browser admins only.
 */
type State = 'idle' | 'loading' | 'shown' | 'disabled' | 'error';

export function MobileAccessCard() {
  const [state, setState] = useState<State>('idle');
  const [token, setToken] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const host = typeof location !== 'undefined' ? location.origin : '';

  async function reveal() {
    setState('loading');
    try {
      const r = await getAdminToken();
      if (!r.enabled || !r.token) {
        setState('disabled');
        return;
      }
      setToken(r.token);
      setState('shown');
    } catch {
      setState('error');
    }
  }

  async function copy(label: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(label);
      window.setTimeout(() => setCopied((c) => (c === label ? null : c)), 1500);
    } catch {
      /* clipboard blocked — the value is visible to select manually */
    }
  }

  return (
    <section class="mobile-access">
      <div class="mobile-access-head">
        <h2>Mobile access</h2>
        {state === 'idle' && (
          <button class="btn ghost" onClick={reveal}>
            Reveal token
          </button>
        )}
        {state === 'shown' && (
          <button class="btn ghost" onClick={() => setState('idle')}>
            Hide
          </button>
        )}
      </div>
      <p class="sub">
        Connect the kube-coder mobile app to this controller — add a controller connection with the host and
        token below.
      </p>

      {state === 'loading' && <p class="sub">Loading…</p>}
      {state === 'error' && (
        <div class="banner err" role="alert">
          Couldn't load the token. Try again.
        </div>
      )}
      {state === 'disabled' && (
        <p class="sub">
          Not enabled. Set <code>controller.adminToken.enabled: true</code> in the chart values and redeploy,
          then reveal the token here.
        </p>
      )}
      {state === 'shown' && token && (
        <div class="mobile-access-fields">
          <label>
            <span class="ma-label">Controller host</span>
            <span class="copy-row">
              <code>{host}</code>
              <button class="btn ghost" onClick={() => copy('host', host)}>
                {copied === 'host' ? 'Copied' : 'Copy'}
              </button>
            </span>
          </label>
          <label>
            <span class="ma-label">Controller token</span>
            <span class="copy-row">
              <code class="ma-token">{token}</code>
              <button class="btn ghost" onClick={() => copy('token', token)}>
                {copied === 'token' ? 'Copied' : 'Copy'}
              </button>
            </span>
          </label>
          <p class="sub ma-hint">Treat this token like a password — it grants full admin access.</p>
        </div>
      )}
    </section>
  );
}
