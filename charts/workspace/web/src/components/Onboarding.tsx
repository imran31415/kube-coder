import { useEffect, useRef, useState } from 'preact/hooks';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { githubStatus, setGitConfig, generateSshKey, type GitHubStatus } from '../api/github';
import { createTask } from '../store/tasks';
import { navigate } from '../store/router';
import { pushToast } from '../store/ui';
import { Button } from './primitives/Button';
import { Input } from './primitives/Input';
import { Icon } from './Icon';
import { GithubConnect } from './GithubConnect';
import './Onboarding.css';

const DONE_KEY = 'kc.onboardingDone';

export function Onboarding() {
  const [show, setShow] = useState(false);
  const [step, setStep] = useState(0);
  const [status, setStatus] = useState<GitHubStatus | null>(null);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [prompt, setPrompt] = useState('Take a tour of /home/dev and tell me what kind of projects are here.');
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useFocusTrap(show, ref);

  useEffect(() => {
    if (typeof localStorage === 'undefined') return;
    if (localStorage.getItem(DONE_KEY) === 'true') return;
    githubStatus()
      .then((s) => {
        setStatus(s);
        setName(s.git_user_name ?? '');
        setEmail(s.git_user_email ?? '');
        // Don't auto-open if everything's already set.
        if (!s.ssh_key_exists || !s.git_user_email) setShow(true);
      })
      .catch(() => {
        // Workspace not reachable — skip onboarding silently.
      });
  }, []);

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

  async function createFirstTask() {
    setBusy(true);
    try {
      const t = await createTask({ prompt, workdir: '/home/dev' });
      if (t) {
        navigate('/tasks');
        dismiss();
      }
    } finally {
      setBusy(false);
    }
  }

  if (!show) return null;

  const steps = [
    {
      title: 'Welcome to kube-coder',
      body: (
        <>
          <p>This workspace runs Claude Code in tmux sessions and tracks memory, triggers, and files in one place.</p>
          <p class="muted">Four short steps. You can skip any of them.</p>
        </>
      ),
      action: <Button variant="primary" onClick={() => setStep(1)}>Get started</Button>,
    },
    {
      title: 'Set your git identity',
      body: (
        <>
          <p class="muted">Used for commits Claude makes on your behalf.</p>
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
            Connect your personal GitHub so Claude can push to your repos and use your
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
      title: 'Create your first task',
      body: (
        <>
          <p class="muted">Send Claude a starter prompt. It runs in a tmux session you can watch live.</p>
          <textarea
            class="ob-textarea"
            rows={4}
            aria-label="Starter prompt for your first task"
            value={prompt}
            onInput={(e) => setPrompt((e.target as HTMLTextAreaElement).value)}
          />
        </>
      ),
      action: (
        <>
          <Button variant="ghost" onClick={dismiss}>Finish later</Button>
          <Button variant="primary" disabled={!prompt.trim() || busy} onClick={createFirstTask}>
            <Icon name="play" size={14} /> Create task
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
        <footer class="ob-footer">{s.action}</footer>
        <div class="ob-progress" aria-hidden>
          {steps.map((_, i) => (
            <span key={i} class={`ob-bullet ${i === step ? 'ob-bullet-active' : i < step ? 'ob-bullet-done' : ''}`} />
          ))}
        </div>
      </div>
    </div>
  );
}
