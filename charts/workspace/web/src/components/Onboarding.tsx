import { useEffect, useRef, useState } from 'preact/hooks';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { githubStatus, setGitConfig, generateSshKey, type GitHubStatus } from '../api/github';
import { getHypervisorConfig } from '../api/hypervisor';
import { navigate } from '../store/router';
import { justOnboarded } from '../store/onboarding';
import { pushToast } from '../store/ui';
import { Button } from './primitives/Button';
import { Input } from './primitives/Input';
import { Icon } from './Icon';
import { GithubConnect } from './GithubConnect';
import { ClaudeCredentialSetup } from './ClaudeCredentialSetup';
import { claudeReady, refreshClaudeReady } from '../store/claude';
import './Onboarding.css';

const DONE_KEY = 'kc.onboardingDone';

// Step indices the open logic below jumps to. Named so the fast-path reads as
// intent rather than as magic numbers.
const STEP_WELCOME = 0;
const STEP_IDENTITY = 1;
const STEP_SSH = 3;
const STEP_CLAUDE = 4;

export function Onboarding() {
  const [show, setShow] = useState(false);
  const [step, setStep] = useState(0);
  const [status, setStatus] = useState<GitHubStatus | null>(null);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  // The workspace's default assistant, so the copy names the agent the user
  // will actually meet (Claude by default, e.g. "OpenCode Zen" on a trial
  // workspace, #395). isClaude drives the free-provider upgrade-path note.
  const [assistantLabel, setAssistantLabel] = useState('Claude Code');
  const [assistantIsClaude, setAssistantIsClaude] = useState(true);
  const ref = useRef<HTMLDivElement | null>(null);
  useFocusTrap(show, ref);

  // Where to open (#505). The two probes below race, so each records what it
  // learned and lets openAtFirstGap() decide once the git status is in. Steps
  // the workspace already satisfies are skipped: on a provisioned workspace git
  // identity + SSH are pre-configured and the only real gate is "Connect
  // Claude", which used to sit four clicks deep behind steps with nothing to do.
  const opened = useRef(false);
  const gitProbe = useRef<{ done: boolean; status: GitHubStatus | null }>({
    done: false,
    status: null,
  });
  const claudeGap = useRef(false);

  function openAtFirstGap() {
    if (opened.current) return;
    const { done, status: s } = gitProbe.current;
    if (!done) return; // the git probe owns every step before Claude — wait for it
    let target: number;
    if (s === null) {
      // Workspace unreachable: we can't tell what's configured, so only a known
      // Claude gap opens the wizard — and it opens at the top, as before.
      if (!claudeGap.current) return;
      target = STEP_WELCOME;
    } else {
      const identityOk = !!(s.git_user_name && s.git_user_email);
      const sshOk = !!s.ssh_key_exists;
      if (identityOk && sshOk && !claudeGap.current) return; // nothing to do
      if (!identityOk && !sshOk) target = STEP_WELCOME; // fresh workspace: introduce it
      else if (!identityOk) target = STEP_IDENTITY;
      else if (!sshOk) target = STEP_SSH;
      else target = STEP_CLAUDE; // the beta fast-path: Claude is the only gate
    }
    opened.current = true;
    setStep(target);
    setShow(true);
  }

  useEffect(() => {
    if (typeof localStorage === 'undefined') return;
    if (localStorage.getItem(DONE_KEY) === 'true') return;
    githubStatus()
      .then((s) => {
        setStatus(s);
        setName(s.git_user_name ?? '');
        setEmail(s.git_user_email ?? '');
        gitProbe.current = { done: true, status: s };
        openAtFirstGap();
      })
      .catch(() => {
        // Workspace not reachable — nothing known about git; a Claude gap can
        // still open the wizard (at the top), otherwise stay quiet.
        gitProbe.current = { done: true, status: null };
        openAtFirstGap();
      });
    // Tailor onboarding to the configured default assistant (#395). A trial
    // workspace on a free assistant (e.g. opencode-zen) needs no Claude login,
    // so we must NOT force the Claude-connect flow there — only Claude-default
    // workspaces get the "connect before your first build" gate (#494).
    getHypervisorConfig()
      .then((cfg) => {
        const def = (cfg.assistants ?? []).find((a) => a.id === cfg.defaultAssistant);
        const isClaude = (cfg.defaultAssistant || 'claude') === 'claude';
        setAssistantLabel(def?.label || 'Claude Code');
        setAssistantIsClaude(isClaude);
        if (isClaude) {
          void refreshClaudeReady().then((r) => {
            if (r === false) {
              claudeGap.current = true;
              openAtFirstGap();
            }
          });
        }
      })
      .catch(() => {
        // Config unreachable — assume the Claude default and keep #494 behaviour.
        void refreshClaudeReady().then((r) => {
          if (r === false) {
            claudeGap.current = true;
            openAtFirstGap();
          }
        });
      });
  }, []);

  // Short verb-phrase name ("Claude" reads better than "Claude Code" mid-sentence).
  const assistantName = assistantIsClaude ? 'Claude' : assistantLabel;

  function dismiss() {
    localStorage.setItem(DONE_KEY, 'true');
    setShow(false);
  }

  async function saveIdentity() {
    if (!name.trim() || !email.trim()) return;
    setBusy(true);
    try {
      await setGitConfig(name.trim(), email.trim());
      pushToast('Git identity saved', { kind: 'success' });
      setStep(2);
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Save failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  async function genKey() {
    if (!email.trim()) return;
    setBusy(true);
    try {
      await generateSshKey(email.trim());
      pushToast('SSH key generated', { kind: 'success' });
      const s = await githubStatus();
      setStatus(s);
      setStep(4);
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Keygen failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  // Connected via either path (OAuth or API key) — re-probe readiness so the
  // panel flips to "connected", then advance to the AI CTO front-door step (#494).
  function onClaudeConnected() {
    void refreshClaudeReady().then(() => setStep(5));
  }

  // Land the newly-onboarded user in the AI CTO front door (#487) instead of
  // seeding a Build-tab "tour" task. The CTO welcome shows a warm first-win
  // opener; combined with #486 the user's one-sentence reply builds immediately
  // and #484/#485 auto-surface the preview.
  function enterCto() {
    // Backstop: never route a keyless user into the CTO with no way to build —
    // send them to the connect step instead (#494). Only applies to Claude-
    // default workspaces; a free-assistant trial (#395) can build without a key.
    if (assistantIsClaude && claudeReady.value === false) {
      setStep(4);
      return;
    }
    justOnboarded.value = true;
    navigate('/cto');
    dismiss();
  }

  if (!show) return null;

  const steps = [
    {
      title: 'Welcome to kube-coder',
      body: (
        <>
          <p>This workspace runs {assistantLabel} in tmux sessions and tracks memory, triggers, and files in one place.</p>
          {!assistantIsClaude && (
            <p class="muted">
              It's set up on a free assistant so you can try everything right away —
              no login needed. Want Claude or another provider? Add your own key
              anytime in Settings → Provider keys.
            </p>
          )}
          <p class="muted">A few short steps. You can skip any of them.</p>
        </>
      ),
      action: <Button variant="primary" onClick={() => setStep(1)}>Get started</Button>,
    },
    {
      title: 'Set your git identity',
      body: (
        <>
          <p class="muted">Used for commits {assistantName} makes on your behalf.</p>
          <div class="ob-row">
            <label class="ob-field">
              <span>Name</span>
              <Input fullWidth value={name} onInput={(e) => setName((e.target as HTMLInputElement).value)} placeholder="Your name" />
            </label>
            <label class="ob-field">
              <span>Email</span>
              <Input fullWidth type="email" value={email} onInput={(e) => setEmail((e.target as HTMLInputElement).value)} placeholder="you@example.com" />
            </label>
          </div>
        </>
      ),
      action: (
        <>
          <Button variant="ghost" onClick={() => setStep(2)}>Skip</Button>
          <Button variant="primary" disabled={!name.trim() || !email.trim() || busy} onClick={saveIdentity}>Save & continue</Button>
        </>
      ),
    },
    {
      title: 'Connect your GitHub account',
      body: (
        <>
          <p class="muted">
            Connect your personal GitHub so {assistantName} can push to your repos and use your
            identity. Sign in with your browser — no terminal needed.
          </p>
          <GithubConnect compact onConnected={() => setStep(3)} />
        </>
      ),
      action: (
        <>
          <Button variant="ghost" onClick={() => setStep(3)}>Skip</Button>
          <Button variant="primary" onClick={() => setStep(3)}>Continue</Button>
        </>
      ),
    },
    {
      title: 'Generate an SSH key',
      body: status?.ssh_key_exists ? (
        <p class="muted">A key already exists. The public key is shown in Settings — add it to GitHub if you haven't yet.</p>
      ) : (
        <p class="muted">Creates an ed25519 keypair at ~/.ssh/id_ed25519. You'll add the public half to GitHub afterwards.</p>
      ),
      action: (
        <>
          <Button variant="ghost" onClick={() => setStep(4)}>Skip</Button>
          {!status?.ssh_key_exists ? (
            <Button variant="primary" disabled={!email.trim() || busy} onClick={genKey}>
              <Icon name="plus" size={14} /> Generate key
            </Button>
          ) : (
            <Button variant="primary" onClick={() => setStep(4)}>Continue</Button>
          )}
        </>
      ),
    },
    {
      title: assistantIsClaude ? 'Connect Claude' : `You're all set on ${assistantLabel}`,
      body: (
        <>
          {assistantIsClaude ? (
            <>
              <p class="muted">
                kube-coder builds with Claude, so it needs your account before it can
                do anything. Sign in with your Claude subscription (recommended — no
                key to hunt for) or paste an API key.
              </p>
              <ClaudeCredentialSetup ready={claudeReady.value} onConnected={onClaudeConnected} />
            </>
          ) : (
            <p class="muted">
              This workspace runs on {assistantLabel} — a free assistant — so there's
              nothing to log into. You can start building right away. Prefer Claude or
              another provider? Add your own key anytime in Settings → Provider keys.
            </p>
          )}
        </>
      ),
      // The real CTA lives inside <ClaudeCredentialSetup>, so until Claude is
      // connected the footer had nothing to offer but a ghost "Skip for now"
      // and a muted note — which read as a dead control bar (#505). Suppress it
      // entirely while the panel owns the action ("Skip tour" in the header is
      // still the way out), then show one primary Continue once connected.
      action:
        assistantIsClaude && !claudeReady.value ? null : (
          <Button variant="primary" onClick={() => setStep(5)}>Continue</Button>
        ),
    },
    {
      title: 'Meet your AI CTO',
      body: (
        <>
          <p class="muted">
            You're all set. Your AI CTO already knows this workspace — tell it in
            one sentence what you'd like to build and it starts right away, with a
            live preview surfacing in the chat as it goes.
          </p>
          {assistantIsClaude && claudeReady.value === false && (
            <p class="ob-warn">
              Claude isn't connected yet — connect it first so your first build doesn't fail.
            </p>
          )}
        </>
      ),
      action:
        assistantIsClaude && claudeReady.value === false ? (
          <>
            <Button variant="ghost" onClick={dismiss}>Finish later</Button>
            <Button variant="primary" onClick={() => setStep(4)}>
              <Icon name="link" size={14} /> Connect Claude to start
            </Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={dismiss}>Finish later</Button>
            <Button variant="primary" disabled={busy} onClick={enterCto}>
              <Icon name="cto" size={14} /> Meet your AI CTO
            </Button>
          </>
        ),
    },
  ];

  const s = steps[step];

  return (
    <div ref={ref} class="ob-scrim" role="dialog" aria-modal="true" aria-label={`Onboarding: ${s.title}`}>
      <div class="ob">
        <header class="ob-header">
          <span class="ob-step muted mono">Step {step + 1} of {steps.length}</span>
          <button class="ob-skip" onClick={dismiss}>Skip tour</button>
        </header>
        <h2 class="ob-title">{s.title}</h2>
        <div class="ob-body">{s.body}</div>
        {s.action && <footer class="ob-footer">{s.action}</footer>}
        <div class="ob-progress" aria-hidden>
          {steps.map((_, i) => (
            <span key={i} class={`ob-bullet ${i === step ? 'ob-bullet-active' : i < step ? 'ob-bullet-done' : ''}`} />
          ))}
        </div>
      </div>
    </div>
  );
}
