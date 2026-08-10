import { useEffect, useState } from 'preact/hooks';
import {
  boardCredentials,
  credentialsError,
  refreshCredentials,
  saveCredential,
  removeCredential,
} from '../../store/boards';
import { MutatorOnly } from '../../components/MutatorOnly';
import { ConfirmDialog } from '../../components/ConfirmDialog';

/**
 * Board credentials (#588 Phase 4).
 *
 * These are a SEPARATE store from Settings → provider keys, and the separation
 * is the point rather than an accident: provider keys are injected into every
 * CLI subprocess's env at spawn, which is right for a model API key the agent
 * must use and wrong for a board token the agent must never see. A connector
 * names a credential (`@board-creds/NAME`) and the server resolves it at
 * request time.
 *
 * There is deliberately no way to read a value back — only a last-4 hint,
 * which is enough to tell two tokens apart and not enough to use one.
 */
export function CredentialsPanel() {
  const [name, setName] = useState('');
  const [format, setFormat] = useState<'token' | 'basic'>('token');
  const [username, setUsername] = useState('');
  const [secret, setSecret] = useState('');
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);

  useEffect(() => {
    void refreshCredentials();
  }, []);

  const list = boardCredentials.value;

  return (
    <section class="board-creds">
      <header class="board-review-head">
        <h2>Credentials</h2>
        <p class="board-subtitle">
          Named secrets a connector can reference. The agent never sees a value
          — it names one, and the server resolves it when it makes the request.
        </p>
      </header>

      {credentialsError.value && (
        <p class="board-error">{credentialsError.value}</p>
      )}

      <ul class="board-cred-list">
        {list.length === 0 && (
          <li class="board-empty">
            Nothing stored. Add one here, then point a connector at{' '}
            <code>@board-creds/NAME</code>.
          </li>
        )}
        {list.map((cred) => (
          <li key={cred.name} class="board-cred-row">
            <span class="board-cred-name mono">{cred.name}</span>
            <span class="board-cred-format">{cred.format}</span>
            {cred.username && (
              <span class="board-cred-user">{cred.username}</span>
            )}
            <span class="board-cred-hint mono">{cred.hint}</span>
            <MutatorOnly>
              <button
                type="button"
                class="btn btn-ghost btn-sm"
                onClick={() => setRemoving(cred.name)}
              >
                Remove
              </button>
            </MutatorOnly>
          </li>
        ))}
      </ul>

      <MutatorOnly>
        <form
          class="board-cred-form"
          onSubmit={async (e) => {
            e.preventDefault();
            setSaving(true);
            const err = await saveCredential(name.trim().toUpperCase(), {
              secret,
              format,
              username,
            });
            setSaving(false);
            if (!err) {
              setSecret('');
              setName('');
              setUsername('');
            }
          }}
        >
          <label class="board-run-field">
            <span>Name</span>
            <input
              value={name}
              placeholder="JIRA_API_TOKEN"
              onInput={(e) => setName((e.target as HTMLInputElement).value)}
            />
          </label>
          <label class="board-run-field">
            <span>Type</span>
            <select
              value={format}
              aria-label="Credential type"
              onInput={(e) =>
                setFormat(
                  (e.target as HTMLSelectElement).value as 'token' | 'basic',
                )
              }
            >
              <option value="token">Token (bearer / header)</option>
              <option value="basic">Basic (Jira Cloud: email + token)</option>
            </select>
          </label>
          {format === 'basic' && (
            <label class="board-run-field">
              <span>Username</span>
              <input
                value={username}
                placeholder="you@example.com"
                onInput={(e) =>
                  setUsername((e.target as HTMLInputElement).value)
                }
              />
            </label>
          )}
          <label class="board-run-field">
            <span>Secret</span>
            <input
              type="password"
              value={secret}
              autocomplete="off"
              placeholder="paste the raw token"
              onInput={(e) => setSecret((e.target as HTMLInputElement).value)}
            />
          </label>
          <button
            type="submit"
            class="btn btn-primary btn-sm"
            disabled={saving || !name.trim()}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
          {format === 'basic' && (
            <p class="board-cred-note">
              Paste the <strong>raw</strong> API token — the server composes the
              Basic header. A pre-encoded blob is unverifiable by eye and a
              stray newline fails only at request time.
            </p>
          )}
        </form>
      </MutatorOnly>

      <ConfirmDialog
        open={removing !== null}
        title={`Remove ${removing ?? ''}?`}
        body="Any board referencing this name will stop authenticating."
        confirmLabel="Remove"
        destructive
        onConfirm={async () => {
          const target = removing;
          setRemoving(null);
          if (target) await removeCredential(target);
        }}
        onCancel={() => setRemoving(null)}
      />
    </section>
  );
}
