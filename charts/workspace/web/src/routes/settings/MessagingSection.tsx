import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import {
  getProviders,
  getCredentials,
  putCredentials,
  deleteCredentials,
  testConnection,
  createLink,
  listLinks,
  deleteLink,
  type ProviderSpec,
  type CredentialField,
  type CredentialsView,
  type GatewayLink,
  type PairingCode,
} from '../../api/gateway';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { pushToast } from '../../store/ui';

function fmtCountdown(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

async function copy(text: string, label: string) {
  try {
    await navigator.clipboard.writeText(text);
    pushToast(`${label} copied`, { kind: 'success' });
  } catch {
    pushToast(`Couldn't copy ${label.toLowerCase()} — select it and copy manually`, { kind: 'warn' });
  }
}

/** The dialable WhatsApp number for a wa.me deep link, or null when the sender
 *  isn't a real number (Meta's phone_number_id is an opaque id, not dialable). */
function dialableSender(view: CredentialsView | null): string | null {
  if (!view || !view.sender_field) return null;
  const raw = view.fields[view.sender_field]?.value || '';
  if (!(raw.startsWith('whatsapp:') || raw.startsWith('+'))) return null;
  const digits = raw.replace(/^whatsapp:/, '').replace(/[^\d]/g, '');
  return digits.length >= 6 ? digits : null;
}

export function MessagingSection() {
  const [providers, setProviders] = useState<ProviderSpec[] | null>(null);
  const [available, setAvailable] = useState(true);
  const [cred, setCred] = useState<CredentialsView | null>(null);
  const [providerId, setProviderId] = useState<string>('');
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [links, setLinks] = useState<GatewayLink[]>([]);
  const [code, setCode] = useState<PairingCode | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [unlinkTarget, setUnlinkTarget] = useState<string | null>(null);
  const codeTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const spec = useMemo(
    () => providers?.find((p) => p.id === providerId) ?? null,
    [providers, providerId],
  );

  // Seed the draft inputs from a redacted view: pre-fill non-secret fields with
  // their stored value; leave secret fields blank (the Pill shows set · hint).
  function seedDrafts(view: CredentialsView | null, s: ProviderSpec | null) {
    const next: Record<string, string> = {};
    if (view && s && view.provider_id === s.id) {
      for (const f of [...s.credential_fields, s.sender_field]) {
        if (!f.secret) next[f.key] = view.fields[f.key]?.value ?? '';
      }
    }
    setDrafts(next);
  }

  async function refresh() {
    try {
      const [pr, cr] = await Promise.all([getProviders(), getCredentials()]);
      setProviders(pr.providers);
      setAvailable(pr.available);
      setCred(cr.credentials);
      const pid = cr.credentials.provider_id || pr.providers[0]?.id || '';
      setProviderId(pid);
      seedDrafts(cr.credentials, pr.providers.find((p) => p.id === pid) ?? null);
    } catch {
      // gateway/server unavailable — leave nulls (renders the "unavailable" note)
      setAvailable(false);
    }
  }

  async function refreshLinks() {
    try {
      const r = await listLinks();
      setLinks(r.links || []);
    } catch {
      /* leave prior links */
    }
  }

  useEffect(() => {
    void refresh();
    void refreshLinks();
    return () => {
      if (codeTimer.current) clearInterval(codeTimer.current);
    };
  }, []);

  // Countdown for a freshly minted pairing code.
  useEffect(() => {
    if (codeTimer.current) clearInterval(codeTimer.current);
    if (!code) return;
    codeTimer.current = setInterval(() => {
      setRemaining((r) => {
        if (r <= 1) {
          if (codeTimer.current) clearInterval(codeTimer.current);
          setCode(null);
          return 0;
        }
        return r - 1;
      });
    }, 1000);
    return () => {
      if (codeTimer.current) clearInterval(codeTimer.current);
    };
  }, [code]);

  function onPickProvider(pid: string) {
    setProviderId(pid);
    setTestResult(null);
    seedDrafts(cred, providers?.find((p) => p.id === pid) ?? null);
  }

  async function onSave() {
    if (!spec) return;
    setBusy(true);
    try {
      const creds: Record<string, string> = {};
      for (const f of spec.credential_fields) creds[f.key] = drafts[f.key] ?? '';
      await putCredentials({
        provider_id: spec.id,
        creds,
        sender_number: drafts[spec.sender_field.key] ?? '',
      });
      pushToast('Credentials saved', { kind: 'success' });
      setTestResult(null);
      await refresh();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Save failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  async function onDisconnect() {
    setConfirmDisconnect(false);
    setBusy(true);
    try {
      await deleteCredentials();
      pushToast('Messaging disconnected', { kind: 'info' });
      setTestResult(null);
      await refresh();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Disconnect failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  async function onTest() {
    setBusy(true);
    setTestResult(null);
    try {
      setTestResult(await testConnection());
    } catch (err) {
      setTestResult({ ok: false, detail: err instanceof Error ? err.message : 'Test failed' });
    } finally {
      setBusy(false);
    }
  }

  async function onLink() {
    setBusy(true);
    try {
      const c = await createLink();
      setCode(c);
      setRemaining(c.expires_in || 600);
      await refreshLinks();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Could not create a pairing code', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  async function onUnlink(id: string) {
    setUnlinkTarget(null);
    setBusy(true);
    try {
      await deleteLink(id);
      pushToast('Unlinked', { kind: 'info' });
      await refreshLinks();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Unlink failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  const configured = !!cred?.configured;
  const webhookUrl =
    typeof window !== 'undefined'
      ? `https://${window.location.host}/api/gateway/whatsapp/webhook`
      : '';
  const verifyToken =
    spec?.credential_fields.some((f) => f.key === 'verify_token')
      ? cred?.fields['verify_token']?.value || (drafts['verify_token'] ?? '')
      : '';
  const sender = dialableSender(cred);

  function fieldRow(f: CredentialField) {
    const state = cred?.provider_id === spec?.id ? cred?.fields[f.key] : undefined;
    return (
      <div class="settings-row" key={f.key}>
        <div class="settings-row-label">
          {f.label}{' '}
          {f.secret ? (
            <Pill tone={state?.set ? 'success' : 'warn'} mono>
              {state?.set ? `set · ${state.hint || '••••'}` : 'not set'}
            </Pill>
          ) : null}
          {f.help_url ? (
            <div class="settings-radio-hint muted">
              <a href={f.help_url} target="_blank" rel="noreferrer">Where to find this ↗</a>
            </div>
          ) : null}
        </div>
        <div class="settings-row-control" style={{ gap: 'var(--size-2)' }}>
          <Input
            fullWidth
            type={f.secret ? 'password' : 'text'}
            value={drafts[f.key] ?? ''}
            placeholder={f.secret ? (state?.set ? 'Replace…' : f.placeholder || 'Paste…') : f.placeholder}
            onInput={(e) => {
              const v = (e.target as HTMLInputElement).value;
              setDrafts((d) => ({ ...d, [f.key]: v }));
            }}
          />
        </div>
      </div>
    );
  }

  return (
    <section class="settings-section">
      <h2 class="settings-section-title">Messaging / WhatsApp</h2>
      <p class="settings-row-hint muted">
        Connect your own WhatsApp provider to chat with this workspace's agent over WhatsApp. Enter
        your provider credentials, paste the webhook URL into the provider console, then link your
        number. Credentials are stored securely on your workspace disk and never shared.
      </p>

      {!available ? (
        <p class="settings-row-hint muted">The messaging gateway is not available on this workspace.</p>
      ) : !providers ? (
        <p class="settings-row-hint muted">Loading…</p>
      ) : (
        <>
          {/* Provider picker */}
          <div class="settings-row">
            <div class="settings-row-label">Provider</div>
            <div class="settings-row-control">
              <div class="seg">
                {providers.map((p) => (
                  <button
                    key={p.id}
                    class={`seg-item ${providerId === p.id ? 'seg-item-active' : ''}`}
                    onClick={() => onPickProvider(p.id)}
                  >
                    {p.display_name}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Data-driven credential fields + sender */}
          {spec ? (
            <>
              {spec.credential_fields.map(fieldRow)}
              {fieldRow(spec.sender_field)}

              <div class="settings-row">
                <div class="settings-row-label">
                  Status{' '}
                  <Pill tone={configured ? 'success' : 'warn'} mono>
                    {configured ? 'connected' : 'not configured'}
                  </Pill>
                </div>
                <div class="settings-row-control" style={{ gap: 'var(--size-2)' }}>
                  <Button variant="primary" type="button" disabled={busy} onClick={onSave}>
                    <Icon name="check" size={14} /> Save
                  </Button>
                  <Button
                    variant="secondary"
                    type="button"
                    disabled={busy || !configured}
                    onClick={onTest}
                  >
                    Test connection
                  </Button>
                  {configured ? (
                    <Button
                      variant="secondary"
                      type="button"
                      disabled={busy}
                      onClick={() => setConfirmDisconnect(true)}
                    >
                      Disconnect
                    </Button>
                  ) : null}
                </div>
              </div>

              {testResult ? (
                <div class="settings-row">
                  <div class="settings-row-label" />
                  <div class="settings-row-control">
                    <Pill tone={testResult.ok ? 'success' : 'danger'} mono>
                      {testResult.ok ? 'connection ok' : 'failed'} · {testResult.detail}
                    </Pill>
                  </div>
                </div>
              ) : !configured ? (
                <p class="settings-row-hint muted">Save credentials first, then test the connection.</p>
              ) : null}

              {/* Webhook helper */}
              <div class="settings-row">
                <div class="settings-row-label">Webhook URL</div>
                <div class="settings-row-control settings-copy-row">
                  <Input fullWidth readOnly value={webhookUrl} />
                  <Button onClick={() => copy(webhookUrl, 'Webhook URL')}>
                    <Icon name="link" size={14} /> Copy
                  </Button>
                </div>
              </div>
              {verifyToken ? (
                <div class="settings-row">
                  <div class="settings-row-label">Verify token</div>
                  <div class="settings-row-control settings-copy-row">
                    <Input fullWidth readOnly value={verifyToken} />
                    <Button onClick={() => copy(verifyToken, 'Verify token')}>
                      <Icon name="link" size={14} /> Copy
                    </Button>
                  </div>
                </div>
              ) : null}
              <p class="settings-row-hint muted">
                Paste the webhook URL{verifyToken ? ' and verify token' : ''} into your provider
                console to receive inbound messages.
              </p>

              {/* Link management */}
              <div class="settings-row">
                <div class="settings-row-label">Link a number</div>
                <div class="settings-row-control">
                  <Button variant="primary" type="button" disabled={busy} onClick={onLink}>
                    <Icon name="link" size={14} /> Link WhatsApp
                  </Button>
                </div>
              </div>

              {code ? (
                <div class="settings-row">
                  <div class="settings-row-label">
                    Pairing code
                    <div class="settings-radio-hint muted">expires in {fmtCountdown(remaining)}</div>
                  </div>
                  <div class="settings-row-control settings-copy-row">
                    <Input fullWidth readOnly value={code.code} />
                    <Button onClick={() => copy(code.code, 'Code')}>
                      <Icon name="link" size={14} /> Copy
                    </Button>
                    {sender ? (
                      <a
                        class="btn btn-secondary btn-md"
                        href={`https://wa.me/${sender}?text=${encodeURIComponent(code.code)}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open in WhatsApp
                      </a>
                    ) : null}
                  </div>
                </div>
              ) : null}
              {code && !sender ? (
                <p class="settings-row-hint muted">
                  Message your WhatsApp Business number with the code above to link this number.
                </p>
              ) : null}

              {/* Bindings */}
              {links.length > 0 ? (
                <div class="settings-row settings-row-control-stack">
                  <div class="settings-row-label">Linked numbers</div>
                  <div class="settings-row-control settings-row-control-stack">
                    {links.map((l) =>
                      l.bindings.map((b) => (
                        <div class="settings-link-row" key={`${l.id}-${b.workspace}`}>
                          <div>
                            <div class="settings-link-ws">{b.workspace}</div>
                            <div class="settings-radio-hint muted">
                              {b.workspace_host || '—'}
                              {b.is_default ? ' · default' : ''}
                            </div>
                          </div>
                          <Button
                            variant="secondary"
                            type="button"
                            disabled={busy}
                            onClick={() => setUnlinkTarget(l.id)}
                          >
                            Unlink
                          </Button>
                        </div>
                      )),
                    )}
                  </div>
                </div>
              ) : null}
            </>
          ) : null}
        </>
      )}

      <ConfirmDialog
        open={confirmDisconnect}
        title="Disconnect messaging?"
        body="This clears your stored provider credentials for this workspace and disables the WhatsApp channel. You can reconnect any time."
        confirmLabel="Disconnect"
        destructive
        onConfirm={onDisconnect}
        onCancel={() => setConfirmDisconnect(false)}
      />
      <ConfirmDialog
        open={unlinkTarget !== null}
        title="Unlink this number?"
        body="The bound WhatsApp number will stop reaching this workspace until it links again."
        confirmLabel="Unlink"
        destructive
        onConfirm={() => unlinkTarget && onUnlink(unlinkTarget)}
        onCancel={() => setUnlinkTarget(null)}
      />
    </section>
  );
}
