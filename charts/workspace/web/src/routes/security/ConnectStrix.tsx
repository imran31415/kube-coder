import { useEffect, useState } from 'preact/hooks';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { MutatorOnly } from '../../components/MutatorOnly';
import {
  abandonSignIn,
  beginSignIn,
  connection,
  connectionBusy,
  connectionResult,
  forgetConnection,
  loadConnection,
  saveConnection,
  signInBusy,
  signInError,
  signInUrl,
  signOutSubscription,
  submitSignIn,
  testConnection,
} from '../../store/scans';

/**
 * Connect a model to the scanner (#726).
 *
 * The scanner needs an AI model of the user's own choosing, so this asks the
 * same three things the scanner's own configuration does — model, key, and an
 * optional server address for a locally-run model — and saves them where the
 * scanner already looks. There is deliberately no list of approved models and
 * no default: which model to spend money on is the user's decision, and the
 * scanner warns about weak ones itself.
 *
 * The key is write-only from the browser's point of view. It is sent here and
 * never comes back; the server reports only whether one is stored.
 */
export function ConnectStrix({ onDone }: { onDone?: () => void }) {
  const conn = connection.value;
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [apiBase, setApiBase] = useState('');
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    void loadConnection();
  }, []);

  // Seed the fields from what is saved, but never stomp on typing in progress.
  useEffect(() => {
    if (touched || !conn) return;
    setModel(conn.model);
    setApiBase(conn.api_base);
  }, [conn?.model, conn?.api_base, touched]);

  const install = conn?.install;
  const usesSubscription = model.trim().startsWith('chatgpt/');
  const result = connectionResult.value;

  async function save(e: Event) {
    e.preventDefault();
    const body: { model: string; api_base: string; api_key?: string } = {
      model: model.trim(),
      api_base: apiBase.trim(),
    };
    // Only send the key when one was typed, so saving a new model does not
    // require the browser to have held it.
    if (apiKey) body.api_key = apiKey;
    if (await saveConnection(body)) {
      setApiKey('');
      setTouched(false);
      await testConnection();
      onDone?.();
    }
  }

  return (
    <section class="security-connect">
      <header class="security-connect-head">
        <h2>Connect a model</h2>
        <p class="muted">
          Scanning is done by an AI model that you choose and pay for. Your key
          is stored in this workspace only, and is kept separate from the one
          your assistant uses — so a long scan can never use up the assistant's
          budget.
        </p>
      </header>

      {install && install.state !== 'ready' ? (
        <p class={`security-install security-install-${install.state}`}>
          {install.state === 'installing'
            ? 'Setting up the scanner in this workspace… this happens once and takes a few minutes.'
            : install.state === 'failed'
              ? `The scanner could not be set up: ${install.error}`
              : 'The scanner will be set up the first time you save a model here.'}
        </p>
      ) : null}

      <form class="security-form" onSubmit={save}>
        <label class="security-field">
          <span>Model</span>
          <Input
            value={model}
            placeholder="provider/model-name"
            onInput={(e) => {
              setTouched(true);
              setModel((e.target as HTMLInputElement).value);
            }}
          />
          <small class="muted">
            Written the way the scanner expects — for example{' '}
            <code>openrouter/…</code>, <code>anthropic/…</code>,{' '}
            <code>openai/…</code>, or <code>ollama/…</code> for a model running
            on this machine. A stronger model finds more; the scanner will warn
            you if it considers the one you picked too weak.
          </small>
        </label>

        {usesSubscription ? (
          <SubscriptionSignIn signedIn={!!conn?.subscription?.signed_in} />
        ) : (
          <label class="security-field">
            <span>API key</span>
            <Input
              type="password"
              value={apiKey}
              placeholder={conn?.has_key ? 'Saved — type to replace' : 'Paste your key'}
              autocomplete="off"
              onInput={(e) => {
                setTouched(true);
                setApiKey((e.target as HTMLInputElement).value);
              }}
            />
            <small class="muted">
              Stored in this workspace and never shown again.
            </small>
          </label>
        )}

        <label class="security-field">
          <span>Server address <span class="muted">(optional)</span></span>
          <Input
            value={apiBase}
            placeholder="http://localhost:11434"
            onInput={(e) => {
              setTouched(true);
              setApiBase((e.target as HTMLInputElement).value);
            }}
          />
          <small class="muted">Only for a model running on this machine.</small>
        </label>

        <div class="security-actions">
          <MutatorOnly>
            <Button
              type="submit"
              variant="primary"
              disabled={connectionBusy.value || !model.trim()}
            >
              {connectionBusy.value ? 'Saving…' : 'Save'}
            </Button>
          </MutatorOnly>
          <MutatorOnly>
            <Button
              type="button"
              onClick={() => void testConnection()}
              disabled={connectionBusy.value || !conn?.configured}
            >
              Check connection
            </Button>
          </MutatorOnly>
          {conn?.configured ? (
            <MutatorOnly>
              <Button
                type="button"
                variant="ghost"
                onClick={() => void forgetConnection()}
                disabled={connectionBusy.value}
              >
                Disconnect
              </Button>
            </MutatorOnly>
          ) : null}
        </div>
      </form>

      {result ? (
        <p class={`security-result ${result.ok ? 'is-ok' : 'is-bad'}`}>
          <Pill tone={result.ok ? 'success' : 'danger'}>
            {result.ok ? 'Connected' : 'Not working'}
          </Pill>{' '}
          {result.detail}
        </p>
      ) : null}
    </section>
  );
}

/**
 * Signing in to a ChatGPT subscription, without a browser in the workspace.
 *
 * The sign-in normally finishes by redirecting to a local address on the
 * machine running the scanner — which is the workspace, while the browser is
 * on the user's own computer, so that redirect can never arrive. The scanner
 * has a mode for exactly this: it prints a link, and waits for the address the
 * browser ended up at to be pasted back.
 *
 * So: open the link, sign in as normal, copy the address bar, paste it here.
 * What is pasted is forwarded straight to the waiting scanner and kept
 * nowhere.
 */
function SubscriptionSignIn({ signedIn }: { signedIn: boolean }) {
  const [pasted, setPasted] = useState('');
  const url = signInUrl.value;

  if (signedIn && !url) {
    return (
      <div class="security-note">
        <p>
          <Pill tone="success">Signed in</Pill> This workspace is signed in to a
          ChatGPT subscription, so no API key is needed for this model.
        </p>
        <Button
          type="button"
          variant="ghost"
          onClick={() => void signOutSubscription()}
          disabled={connectionBusy.value}
        >
          Sign out
        </Button>
      </div>
    );
  }

  if (!url) {
    return (
      <div class="security-note">
        <p>
          This model runs on a ChatGPT subscription, so it needs a sign-in
          rather than a key.
        </p>
        <MutatorOnly>
          <Button
            type="button"
            onClick={() => void beginSignIn()}
            disabled={signInBusy.value}
          >
            {signInBusy.value ? 'Starting…' : 'Sign in with ChatGPT'}
          </Button>
        </MutatorOnly>
        {signInError.value ? (
          <p class="security-warn">{signInError.value}</p>
        ) : null}
      </div>
    );
  }

  return (
    <div class="security-note">
      <ol class="security-steps">
        <li>
          <a href={url} target="_blank" rel="noopener noreferrer">
            Open the sign-in page
          </a>{' '}
          and sign in as usual.
        </li>
        <li>
          When it finishes, your browser lands on a page that will not load.
          That is expected — copy the whole address from the address bar.
        </li>
        <li>Paste it below.</li>
      </ol>
      <Input
        value={pasted}
        placeholder="Paste the address you landed on"
        autocomplete="off"
        onInput={(e) => setPasted((e.target as HTMLInputElement).value)}
      />
      <div class="security-actions">
        <Button
          type="button"
          variant="primary"
          disabled={signInBusy.value || !pasted.trim()}
          onClick={() => {
            void submitSignIn(pasted.trim());
            setPasted('');
          }}
        >
          Finish sign-in
        </Button>
        <Button type="button" variant="ghost" onClick={() => void abandonSignIn()}>
          Cancel
        </Button>
      </div>
      {signInError.value ? <p class="security-warn">{signInError.value}</p> : null}
    </div>
  );
}
