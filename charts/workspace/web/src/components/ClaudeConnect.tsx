import { useEffect, useRef, useState } from 'preact/hooks';
import {
  startClaudeConnect,
  submitClaudeConnectCode,
  pollClaudeConnect,
  cancelClaudeConnect,
  type SubscriptionsView,
} from '../api/subscriptions';
import { Button } from './primitives/Button';
import { Input } from './primitives/Input';
import { Icon } from './Icon';
import { pushToast } from '../store/ui';
// Same visual language as the GitHub connect flow — shared ghc-* classes.
import './GithubConnect.css';

type Phase = 'idle' | 'starting' | 'awaiting' | 'verifying' | 'done' | 'error';

const POLL_MS = 1500;
// The CLI exchanges the code within a few seconds; if nothing resolves in a
// minute the session is stuck and the user should start over.
const VERIFY_TIMEOUT_MS = 60_000;

export interface ClaudeConnectProps {
  /** Called after a successful connect with the refreshed subscription view. */
  onConnected?: (subs?: SubscriptionsView) => void;
  /** True when re-authing an expired login — only changes the button label. */
  reconnect?: boolean;
}

/**
 * Browser-less "Connect Claude account" flow.
 *
 * Drives the server-side `claude auth login` OAuth flow so a first-time user
 * never has to open a terminal: click Connect, we open the claude.ai sign-in
 * page in THEIR browser (the paste-code OAuth flow needs no callback into the
 * pod), they approve and paste the one-time code here, and the CLI completes
 * the exchange. Reverse of the GitHub device flow: there the user carries a
 * code TO github.com, here they bring one BACK from claude.ai.
 */
export function ClaudeConnect({ onConnected, reconnect }: ClaudeConnectProps) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [url, setUrl] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const phaseRef = useRef<Phase>('idle');
  phaseRef.current = phase;

  function stopPolling() {
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
  }

  // Stop polling + abort the server session if we unmount mid-flow.
  useEffect(() => () => {
    stopPolling();
    if (phaseRef.current === 'starting' || phaseRef.current === 'awaiting'
      || phaseRef.current === 'verifying') {
      void cancelClaudeConnect().catch(() => {});
    }
  }, []);

  async function begin() {
    setError('');
    setCode('');
    setPhase('starting');
    try {
      const s = await startClaudeConnect();
      setUrl(s.url);
      setPhase('awaiting');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start Claude sign-in');
      setPhase('error');
    }
  }

  async function submit() {
    const trimmed = code.trim();
    if (!trimmed) return;
    setError('');
    setPhase('verifying');
    try {
      await submitClaudeConnectCode(trimmed);
    } catch (err) {
      // Bad paste (validation) — stay on the code step so they can retry.
      setError(err instanceof Error ? err.message : 'Could not submit the code');
      setPhase('awaiting');
      return;
    }
    startPolling();
  }

  function startPolling() {
    stopPolling();
    const deadline = Date.now() + VERIFY_TIMEOUT_MS;
    timer.current = setInterval(async () => {
      try {
        const r = await pollClaudeConnect();
        if (r.connected) {
          stopPolling();
          setPhase('done');
          pushToast('Claude account connected', { kind: 'success' });
          onConnected?.(r.subscriptions);
        } else if (!r.in_progress) {
          stopPolling();
          setError(r.error || 'Claude sign-in did not complete. Please try again.');
          setPhase('error');
        } else if (Date.now() > deadline) {
          stopPolling();
          void cancelClaudeConnect().catch(() => {});
          setError('Timed out waiting for the sign-in to complete. Please try again.');
          setPhase('error');
        }
      } catch {
        // transient — keep polling
      }
    }, POLL_MS);
  }

  function reset() {
    stopPolling();
    void cancelClaudeConnect().catch(() => {});
    setPhase('idle');
    setUrl('');
    setCode('');
    setError('');
  }

  if (phase === 'done') {
    return (
      <div class="ghc ghc-compact">
        <p class="ghc-connected">
          <Icon name="check" size={14} /> Claude account connected. Assistants now use your subscription.
        </p>
      </div>
    );
  }

  if (phase === 'awaiting' || phase === 'verifying') {
    return (
      <div class="ghc ghc-compact">
        <p class="ghc-lead">
          1. Open the sign-in page and approve, then 2. paste the code Claude shows you.
        </p>
        <div class="ghc-actions">
          <a class="btn btn-primary btn-md" href={url} target="_blank" rel="noopener noreferrer">
            <Icon name="link" size={14} /> Open Claude to sign in
          </a>
        </div>
        <div class="ghc-code-row">
          <Input
            value={code}
            placeholder="Paste authorization code"
            disabled={phase === 'verifying'}
            onInput={(e) => setCode((e.target as HTMLInputElement).value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void submit(); }}
          />
          <Button
            variant="primary"
            disabled={phase === 'verifying' || !code.trim()}
            onClick={() => void submit()}
          >
            {phase === 'verifying' ? 'Connecting…' : 'Connect'}
          </Button>
          <Button variant="ghost" disabled={phase === 'verifying'} onClick={reset}>Cancel</Button>
        </div>
        {error && <p class="ghc-error">{error}</p>}
        {phase === 'verifying' && (
          <p class="ghc-waiting muted">
            <span class="ghc-spinner" aria-hidden /> Confirming with Claude…
          </p>
        )}
      </div>
    );
  }

  return (
    <div class="ghc ghc-compact">
      {phase === 'error' && error && <p class="ghc-error">{error}</p>}
      <Button variant="primary" onClick={begin} disabled={phase === 'starting'}>
        {phase === 'starting' ? 'Starting…'
          : phase === 'error' ? 'Try again'
            : reconnect ? 'Reconnect Claude account' : 'Connect Claude account'}
      </Button>
      {phase !== 'error' && (
        <p class="ghc-hint muted">
          Use your Claude Pro/Max subscription — sign in with your browser, no terminal needed.
        </p>
      )}
    </div>
  );
}
