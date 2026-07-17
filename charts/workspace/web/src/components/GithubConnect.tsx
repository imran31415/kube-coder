import { useEffect, useRef, useState } from 'preact/hooks';
import {
  startGithubConnect,
  pollGithubConnect,
  cancelGithubConnect,
} from '../api/github';
import { Button } from './primitives/Button';
import { Icon } from './Icon';
import { pushToast } from '../store/ui';
import './GithubConnect.css';

type Phase = 'idle' | 'starting' | 'awaiting' | 'done' | 'error';

const POLL_MS = 2500;

export interface GithubConnectProps {
  /** True when the workspace is already on a personal GitHub login. */
  connected?: boolean;
  /** Handle of the connected account, shown in the "already connected" state. */
  user?: string | null;
  /** Called after a successful connect (the server has switched to personal
   *  mode). Parents refresh their GitHub status from here. */
  onConnected?: (user?: string) => void;
  /** Compact styling for tight spots (e.g. the onboarding step). */
  compact?: boolean;
}

/**
 * Browser-less "Connect GitHub account" flow (issue #303).
 *
 * Drives the server-side `gh auth login --web` device flow so a first-time
 * user never has to open a terminal: click Connect, we show the one-time code
 * with a one-tap link to github.com/login/device, then poll until GitHub
 * confirms and the workspace flips to a personal login automatically.
 *
 * Rendered inline (not a modal) so it composes both inside Settings and inside
 * the onboarding dialog without nesting scrims.
 */
export function GithubConnect({ connected, user, onConnected, compact }: GithubConnectProps) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [code, setCode] = useState('');
  const [verifyUrl, setVerifyUrl] = useState('https://github.com/login/device');
  const [error, setError] = useState('');
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  // Track the live phase for the unmount cleanup without re-registering it
  // (a [phase] effect would clear the poll interval on every phase change).
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
    if (phaseRef.current === 'awaiting' || phaseRef.current === 'starting') {
      void cancelGithubConnect().catch(() => {});
    }
  }, []);

  async function begin() {
    setError('');
    setPhase('starting');
    try {
      const s = await startGithubConnect();
      setCode(s.code);
      setVerifyUrl(s.verification_uri || 'https://github.com/login/device');
      setPhase('awaiting');
      startPolling();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start GitHub sign-in');
      setPhase('error');
    }
  }

  function startPolling() {
    stopPolling();
    timer.current = setInterval(async () => {
      try {
        const r = await pollGithubConnect();
        if (r.connected) {
          stopPolling();
          setPhase('done');
          pushToast('GitHub connected', { kind: 'success' });
          onConnected?.(r.connected_user);
        } else if (!r.in_progress) {
          stopPolling();
          setError(r.error || 'GitHub sign-in did not complete. Please try again.');
          setPhase('error');
        }
      } catch {
        // transient — keep polling
      }
    }, POLL_MS);
  }

  function reset() {
    stopPolling();
    void cancelGithubConnect().catch(() => {});
    setPhase('idle');
    setCode('');
    setError('');
  }

  if (connected && phase !== 'done') {
    return (
      <div class={`ghc ${compact ? 'ghc-compact' : ''}`}>
        <p class="ghc-connected">
          <Icon name="check" size={14} /> Connected as <strong>{user || 'your GitHub account'}</strong>
        </p>
        <Button size="sm" variant="ghost" onClick={begin}>Reconnect a different account</Button>
      </div>
    );
  }

  if (phase === 'done') {
    return (
      <div class={`ghc ${compact ? 'ghc-compact' : ''}`}>
        <p class="ghc-connected">
          <Icon name="check" size={14} /> GitHub connected. Git &amp; the CLI now use your account.
        </p>
      </div>
    );
  }

  if (phase === 'awaiting') {
    return (
      <div class={`ghc ${compact ? 'ghc-compact' : ''}`}>
        <p class="ghc-lead">
          1. Copy this one-time code, then 2. open GitHub and paste it to authorize.
        </p>
        <div class="ghc-code-row">
          <code class="ghc-code mono">{code}</code>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              navigator.clipboard?.writeText(code);
              pushToast('Code copied', { kind: 'info' });
            }}
          >
            Copy
          </Button>
        </div>
        <div class="ghc-actions">
          <a class="btn btn-primary btn-md" href={verifyUrl} target="_blank" rel="noopener noreferrer">
            <Icon name="link" size={14} /> Open GitHub to authorize
          </a>
          <Button variant="ghost" onClick={reset}>Cancel</Button>
        </div>
        <p class="ghc-waiting muted">
          <span class="ghc-spinner" aria-hidden /> Waiting for you to authorize on GitHub…
        </p>
      </div>
    );
  }

  return (
    <div class={`ghc ${compact ? 'ghc-compact' : ''}`}>
      {phase === 'error' && error && <p class="ghc-error">{error}</p>}
      <Button
        variant="primary"
        onClick={begin}
        disabled={phase === 'starting'}
      >
        <Icon name="github" size={14} />
        {phase === 'starting' ? ' Starting…' : phase === 'error' ? ' Try again' : ' Connect GitHub account'}
      </Button>
      {phase !== 'error' && (
        <p class="ghc-hint muted">
          Sign in with your browser — no terminal or <code class="mono">gh auth login</code> needed.
        </p>
      )}
    </div>
  );
}
