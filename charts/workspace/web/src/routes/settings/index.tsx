import { theme, density, type Theme, type Density, pushToast } from '../../store/ui';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { GitSection } from './GitSection';
import { BrowserSection } from './BrowserSection';
import { MetricsSection } from './MetricsSection';
import './settings.css';

const THEMES: { id: Theme; label: string }[] = [
  { id: 'system', label: 'System' },
  { id: 'dark', label: 'Dark' },
  { id: 'light', label: 'Light' },
];

const DENSITIES: { id: Density; label: string; hint: string }[] = [
  { id: 'comfortable', label: 'Comfortable', hint: 'Default — more whitespace between rows.' },
  { id: 'compact', label: 'Compact', hint: 'Denser lists for power users.' },
];

export function SettingsRoute() {
  return (
    <div class="route route-settings">
      <header class="route-header">
        <h1 class="route-title">Settings</h1>
        <p class="route-subtitle muted">Appearance, identity, and integration. More sections land in Phase 5.</p>
      </header>

      <section class="settings-section">
        <h2 class="settings-section-title">Appearance</h2>
        <p class="settings-row-hint muted">Preferences persist in your browser only.</p>
        <div class="settings-row">
          <div class="settings-row-label">Theme</div>
          <div class="settings-row-control">
            <div class="seg">
              {THEMES.map((t) => (
                <button
                  key={t.id}
                  class={`seg-item ${theme.value === t.id ? 'seg-item-active' : ''}`}
                  onClick={() => (theme.value = t.id)}
                >
                  {t.id === 'dark' && <Icon name="moon" size={14} />}
                  {t.id === 'light' && <Icon name="sun" size={14} />}
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div class="settings-row">
          <div class="settings-row-label">Density</div>
          <div class="settings-row-control settings-row-control-stack">
            {DENSITIES.map((d) => (
              <label key={d.id} class="settings-radio">
                <input
                  type="radio"
                  name="density"
                  checked={density.value === d.id}
                  onChange={() => (density.value = d.id)}
                />
                <div>
                  <div class="settings-radio-label">{d.label}</div>
                  <div class="settings-radio-hint muted">{d.hint}</div>
                </div>
              </label>
            ))}
          </div>
        </div>
      </section>

      <MetricsSection />

      <GitSection />

      <BrowserSection />

      <section class="settings-section">
        <h2 class="settings-section-title">Diagnostics</h2>
        <div class="settings-row">
          <div class="settings-row-label">Send test toast</div>
          <div class="settings-row-control">
            <Button onClick={() => pushToast('Toast plumbing works.', { kind: 'success' })}>
              <Icon name="check" size={14} /> Test toast
            </Button>
          </div>
        </div>
      </section>
    </div>
  );
}
