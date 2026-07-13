import { useEffect, useState } from 'preact/hooks';
import {
  listProviderKeys,
  setProviderKey,
  deleteProviderKey,
  type ProviderVar,
  type ProviderKeysView,
} from '../../api/providerKeys';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { pushToast } from '../../store/ui';

const PROVIDERS: { var: ProviderVar; label: string; hint: string }[] = [
  { var: 'OPENROUTER_API_KEY', label: 'OpenRouter', hint: 'Powers OpenCode + OpenRouter-backed models.' },
  { var: 'DEEPSEEK_API_KEY', label: 'DeepSeek', hint: 'DeepSeek API key.' },
  { var: 'ANTHROPIC_API_KEY', label: 'Anthropic', hint: 'Overrides the Claude subscription/oauth default when set.' },
];

export function ProviderKeysSection() {
  const [view, setView] = useState<ProviderKeysView | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  async function refresh() {
    try {
      const r = await listProviderKeys();
      setView(r.providers);
    } catch {
      // server unavailable — leave view null
    }
  }
  useEffect(() => { void refresh(); }, []);

  async function onSave(p: ProviderVar) {
    const key = (drafts[p] ?? '').trim();
    if (!key) return;
    setBusy(p);
    try {
      await setProviderKey(p, key);
      pushToast('Key saved', { kind: 'success' });
      setDrafts((d) => ({ ...d, [p]: '' }));
      await refresh();
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
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Clear failed', { kind: 'danger' });
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
    </section>
  );
}
