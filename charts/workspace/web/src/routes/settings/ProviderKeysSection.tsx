import { useEffect, useState } from 'preact/hooks';
import {
  listProviderKeys,
  setProviderKey,
  deleteProviderKey,
  type ProviderVar,
  type ProviderKeysView,
} from '../../api/providerKeys';
import {
  getSubscriptions,
  logoutSubscription,
  type SubscriptionProvider,
  type SubscriptionsView,
} from '../../api/subscriptions';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { pushToast } from '../../store/ui';

const PROVIDERS: { var: ProviderVar; label: string; hint: string }[] = [
  { var: 'OPENROUTER_API_KEY', label: 'OpenRouter', hint: 'Powers OpenCode + OpenRouter-backed models.' },
  { var: 'DEEPSEEK_API_KEY', label: 'DeepSeek', hint: 'DeepSeek API key.' },
  { var: 'ANTHROPIC_API_KEY', label: 'Anthropic', hint: 'Overrides the Claude subscription/oauth default when set.' },
];

const SUBSCRIPTIONS: { id: SubscriptionProvider; label: string }[] = [
  { id: 'claude', label: 'Claude' },
  { id: 'codex', label: 'Codex' },
];

function formatExpiry(ms: number | null | undefined): string {
  if (!ms) return '';
  try {
    return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  } catch {
    return '';
  }
}

export function ProviderKeysSection() {
  const [view, setView] = useState<ProviderKeysView | null>(null);
  const [subs, setSubs] = useState<SubscriptionsView | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [logoutTarget, setLogoutTarget] = useState<SubscriptionProvider | null>(null);

  async function refresh() {
    try {
      const r = await listProviderKeys();
      setView(r.providers);
    } catch {
      // server unavailable — leave view null
    }
  }
  async function refreshSubs() {
    try {
      const r = await getSubscriptions();
      setSubs(r.subscriptions);
    } catch {
      // server unavailable — leave subs null
    }
  }
  useEffect(() => { void refresh(); void refreshSubs(); }, []);

  async function onSave(p: ProviderVar) {
    const key = (drafts[p] ?? '').trim();
    if (!key) return;
    setBusy(p);
    try {
      await setProviderKey(p, key);
      pushToast('Key saved', { kind: 'success' });
      setDrafts((d) => ({ ...d, [p]: '' }));
      await refresh();
      // A new ANTHROPIC_API_KEY changes whether the Claude subscription is
      // overridden — keep the subscription block in sync.
      await refreshSubs();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Save failed', { kind: 'danger' });
    } finally {
      setBusy(null);
    }
  }

  async function onClear(p: ProviderVar) {
    setBusy(p);
    try {
      await deleteProviderKey(p);
      pushToast('Key cleared', { kind: 'info' });
      await refresh();
      await refreshSubs();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Clear failed', { kind: 'danger' });
    } finally {
      setBusy(null);
    }
  }

  async function onLogout(p: SubscriptionProvider) {
    setBusy(`sub:${p}`);
    setLogoutTarget(null);
    try {
      await logoutSubscription(p);
      pushToast('Logged out', { kind: 'info' });
      await refreshSubs();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Logout failed', { kind: 'danger' });
    } finally {
      setBusy(null);
    }
  }

  return (
    <section class="settings-section">
      <h2 class="settings-section-title">Provider API keys</h2>
      <p class="settings-row-hint muted">
        Set your own model-provider keys for this workspace. Stored securely on your workspace disk
        and used the next time an assistant runs — no redeploy. Leave blank to use the workspace default.
      </p>

      {subs && (
        <div class="settings-subs">
          <div class="settings-subs-title muted">Subscription logins</div>
          {SUBSCRIPTIONS.map((s) => {
            const st = subs[s.id];
            // Codex isn't in older images — hide the row entirely when the CLI
            // is absent and there's nothing to report.
            if (s.id === 'codex' && st?.available === false && !st?.logged_in) return null;
            const busyThis = busy === `sub:${s.id}`;
            let tone: 'success' | 'warn' | 'neutral' = 'neutral';
            let text = 'not signed in';
            if (st?.logged_in) {
              const plan = st.plan ? ` · ${st.plan}` : '';
              if (st.kind === 'api_key') {
                tone = 'success';
                text = 'API key';
              } else if (st.expired) {
                tone = 'warn';
                text = `subscription${plan} · expired`;
              } else {
                tone = 'success';
                const exp = formatExpiry(st.expires_at);
                text = `subscription${plan}${exp ? ` · expires ${exp}` : ''}`;
              }
            }
            return (
              <div class="settings-sub-row" key={s.id}>
                <div class="settings-sub-label">
                  <span class="settings-sub-name">{s.label}</span>
                  <Pill tone={tone} mono>{text}</Pill>
                </div>
                {st?.logged_in && (
                  <div class="settings-sub-control">
                    <Button
                      variant="secondary"
                      type="button"
                      disabled={busyThis}
                      onClick={() => setLogoutTarget(s.id)}
                    >
                      Log out
                    </Button>
                  </div>
                )}
                {st?.overridden_by_key && (
                  <div class="settings-sub-note settings-radio-hint muted">
                    A saved Anthropic API key is overriding this subscription.
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {PROVIDERS.map((prov) => {
        const status = view?.[prov.var];
        return (
          <div class="settings-row" key={prov.var}>
            <div class="settings-row-label">
              {prov.label}
              {' '}
              <Pill tone={status?.set ? 'success' : 'warn'} mono>
                {status?.set ? `set · ${status.hint}` : 'not set'}
              </Pill>
              <div class="settings-radio-hint muted">{prov.hint}</div>
            </div>
            <div class="settings-row-control" style={{ gap: 'var(--size-2)' }}>
              <Input
                fullWidth
                type="password"
                value={drafts[prov.var] ?? ''}
                placeholder={status?.set ? 'Replace key…' : 'Paste key…'}
                onInput={(e) => setDrafts((d) => ({ ...d, [prov.var]: (e.target as HTMLInputElement).value }))}
              />
              <Button
                variant="primary"
                type="button"
                disabled={busy === prov.var || !(drafts[prov.var] ?? '').trim()}
                onClick={() => onSave(prov.var)}
              >
                <Icon name="check" size={14} /> Save
              </Button>
              {status?.set && (
                <Button
                  variant="secondary"
                  type="button"
                  disabled={busy === prov.var}
                  onClick={() => onClear(prov.var)}
                >
                  Clear
                </Button>
              )}
            </div>
          </div>
        );
      })}

      <ConfirmDialog
        open={logoutTarget !== null}
        title="Log out of subscription?"
        body={
          logoutTarget
            ? `This runs the ${logoutTarget} CLI logout and clears its saved credentials in this workspace. Assistants using it will stop working until you sign in again.`
            : ''
        }
        confirmLabel="Log out"
        destructive
        onConfirm={() => logoutTarget && onLogout(logoutTarget)}
        onCancel={() => setLogoutTarget(null)}
      />
    </section>
  );
}
