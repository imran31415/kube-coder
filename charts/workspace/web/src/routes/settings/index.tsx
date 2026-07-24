import { useEffect, useRef } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import { theme, density, type Theme, type Density, pushToast } from '../../store/ui';
import { currentPath, pathSuffix, routeHref, navigate } from '../../store/router';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { Button } from '../../components/primitives/Button';
import { Icon, type IconName } from '../../components/Icon';
import { GitSection } from './GitSection';
import { ProviderKeysSection } from './ProviderKeysSection';
import { McpServersSection } from './McpServersSection';
import { MessagingSection } from './MessagingSection';
import { BrowserSection } from './BrowserSection';
import { MetricsSection } from './MetricsSection';
import { UpdatesSection } from './UpdatesSection';
import { MobileSection } from './MobileSection';
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

interface SettingsGroup {
  /** URL suffix under /settings ('' = the General landing page). */
  slug: string;
  title: string;
  blurb: string;
  icon: IconName;
}

export const SETTINGS_GROUPS: SettingsGroup[] = [
  { slug: '', title: 'General', blurb: 'Appearance and diagnostics.', icon: 'settings' },
  {
    slug: 'account',
    title: 'Account',
    blurb: 'GitHub & SSH identity, mobile app access.',
    icon: 'github',
  },
  {
    slug: 'providers',
    title: 'Providers',
    blurb: 'AI provider keys, subscriptions, and MCP servers.',
    icon: 'apps',
  },
  {
    slug: 'integrations',
    title: 'Integrations',
    blurb: 'Messaging channels like WhatsApp.',
    icon: 'chat',
  },
  {
    slug: 'workspace',
    title: 'Workspace',
    blurb: 'Updates, browser & desktop, system metrics.',
    icon: 'desktop',
  },
];

/** Muscle-memory redirects: `/settings#mcp` → `/settings/providers#mcp`. */
const HASH_GROUP: Record<string, string> = {
  mobile: 'account',
  github: 'account',
  providers: 'providers',
  mcp: 'providers',
  messaging: 'integrations',
  updates: 'workspace',
  browser: 'workspace',
  metrics: 'workspace',
};

function settingsPath(slug: string): string {
  return slug ? `/settings/${slug}` : '/settings';
}

/** Anchor wrapper so deep links like /settings/providers#mcp land on a card
 *  without editing the section components themselves. */
function Anchor({ id, children }: { id: string; children: ComponentChildren }) {
  return (
    <div id={id} class="settings-anchor">
      {children}
    </div>
  );
}

function SettingsNav({ active }: { active: string }) {
  // On narrow viewports the pill row scrolls horizontally — keep the active
  // group's pill visible so the user can tell where they are.
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const pill = navRef.current?.querySelector('.settings-nav-pill-active');
    if (pill && typeof pill.scrollIntoView === 'function') {
      pill.scrollIntoView({ block: 'nearest', inline: 'center' });
    }
  }, [active]);

  return (
    <nav class="settings-nav" aria-label="Settings sections" ref={navRef}>
      {SETTINGS_GROUPS.map((g) => (
        <a
          key={g.slug}
          class={`settings-nav-pill ${active === g.slug ? 'settings-nav-pill-active' : ''}`}
          href={routeHref(settingsPath(g.slug))}
          aria-current={active === g.slug ? 'page' : undefined}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
            e.preventDefault();
            navigate(settingsPath(g.slug));
          }}
        >
          {g.title}
        </a>
      ))}
    </nav>
  );
}

function CategoryList() {
  return (
    <section class="settings-section settings-cats-section">
      <h2 class="settings-section-title">All settings</h2>
      <div class="settings-cats">
        {SETTINGS_GROUPS.filter((g) => g.slug !== '').map((g) => (
          <a
            key={g.slug}
            class="settings-cat"
            href={routeHref(settingsPath(g.slug))}
            onClick={(e) => {
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
              e.preventDefault();
              navigate(settingsPath(g.slug));
            }}
          >
            <span class="settings-cat-icon">
              <Icon name={g.icon} size={18} />
            </span>
            <span class="settings-cat-text">
              <span class="settings-cat-title">{g.title}</span>
              <span class="settings-cat-blurb muted">{g.blurb}</span>
            </span>
            <Icon name="chevron-right" size={16} />
          </a>
        ))}
      </div>
    </section>
  );
}

function AppearanceSection() {
  return (
    <section class="settings-section">
      <h2 class="settings-section-title">Appearance</h2>
      <p class="settings-row-hint muted">Preferences persist in your browser only.</p>
      <div class="settings-row">
        <div class="settings-row-label">Theme</div>
        <div class="settings-row-control">
          <div class="seg" role="group" aria-label="Theme">
            {THEMES.map((t) => (
              <button
                key={t.id}
                class={`seg-item ${theme.value === t.id ? 'seg-item-active' : ''}`}
                aria-pressed={theme.value === t.id}
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
  );
}

function DiagnosticsSection() {
  return (
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
  );
}

function GeneralPage({ isMobile }: { isMobile: boolean }) {
  // Narrow screens get the iOS-Settings-style drill-in list first; on
  // desktop the most-toggled settings (Appearance) stay on top.
  return isMobile ? (
    <>
      <CategoryList />
      <AppearanceSection />
      <DiagnosticsSection />
    </>
  ) : (
    <>
      <AppearanceSection />
      <CategoryList />
      <DiagnosticsSection />
    </>
  );
}

function GroupSections({ slug }: { slug: string }) {
  switch (slug) {
    case 'account':
      return (
        <>
          <Anchor id="github"><GitSection /></Anchor>
          <Anchor id="mobile"><MobileSection /></Anchor>
        </>
      );
    case 'providers':
      return (
        <>
          <Anchor id="providers"><ProviderKeysSection /></Anchor>
          <Anchor id="mcp"><McpServersSection /></Anchor>
        </>
      );
    case 'integrations':
      return (
        <Anchor id="messaging"><MessagingSection /></Anchor>
      );
    case 'workspace':
      return (
        <>
          <Anchor id="updates"><UpdatesSection /></Anchor>
          <Anchor id="browser"><BrowserSection /></Anchor>
          <Anchor id="metrics"><MetricsSection /></Anchor>
        </>
      );
    default:
      return null;
  }
}

export function SettingsRoute() {
  const isMobile = useIsMobile();
  const suffix = pathSuffix(currentPath.value);
  const group = SETTINGS_GROUPS.find((g) => g.slug === suffix) ?? SETTINGS_GROUPS[0];

  // Old-style hash deep links (`/settings#mcp`) redirect to the sub-page that
  // now owns the section, keeping the hash so the anchor scroll below fires.
  useEffect(() => {
    if (suffix !== '') return;
    const hash = window.location.hash.slice(1);
    const target = hash && HASH_GROUP[hash];
    if (!target) return;
    navigate(settingsPath(target), true);
    window.history.replaceState({}, '', routeHref(settingsPath(target)) + `#${hash}`);
  }, [suffix]);

  // Scroll to the anchored section once the group's sections have mounted.
  useEffect(() => {
    const hash = window.location.hash.slice(1);
    if (!hash) return;
    requestAnimationFrame(() => {
      const el = document.getElementById(hash);
      if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView();
    });
  }, [group.slug]);

  // Only render the pill nav on General for desktop — on mobile the category
  // list *is* the navigation and the pills would duplicate it.
  const showNav = !isMobile || group.slug !== '';

  return (
    <div class="route route-settings">
      <header class="route-header">
        <h1 class="route-title">Settings</h1>
        <p class="route-subtitle muted">
          {group.slug === '' ? 'Appearance, diagnostics, and every settings group.' : group.blurb}
        </p>
      </header>

      {showNav && <SettingsNav active={group.slug} />}

      {group.slug === '' ? <GeneralPage isMobile={isMobile} /> : <GroupSections slug={group.slug} />}
    </div>
  );
}
