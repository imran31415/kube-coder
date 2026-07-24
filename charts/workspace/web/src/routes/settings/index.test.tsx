import { render, screen } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import { currentPath, navigate } from '../../store/router';

// The shell test only cares about composition/routing — stub every section so
// no store fetches fire. Section behavior is covered by their own test files.
vi.mock('./GitSection', () => ({ GitSection: () => <div data-testid="sec-git" /> }));
vi.mock('./MobileSection', () => ({ MobileSection: () => <div data-testid="sec-mobile" /> }));
vi.mock('./ProviderKeysSection', () => ({
  ProviderKeysSection: () => <div data-testid="sec-provider-keys" />,
}));
vi.mock('./McpServersSection', () => ({
  McpServersSection: () => <div data-testid="sec-mcp" />,
}));
vi.mock('./MessagingSection', () => ({
  MessagingSection: () => <div data-testid="sec-messaging" />,
}));
vi.mock('./UpdatesSection', () => ({ UpdatesSection: () => <div data-testid="sec-updates" /> }));
vi.mock('./BrowserSection', () => ({ BrowserSection: () => <div data-testid="sec-browser" /> }));
vi.mock('./MetricsSection', () => ({ MetricsSection: () => <div data-testid="sec-metrics" /> }));

import { SettingsRoute } from './index';

const ALL_SECTIONS = [
  'sec-git',
  'sec-mobile',
  'sec-provider-keys',
  'sec-mcp',
  'sec-messaging',
  'sec-updates',
  'sec-browser',
  'sec-metrics',
];

function renderAt(path: string) {
  window.history.replaceState({}, '', path);
  currentPath.value = path;
  return render(<SettingsRoute />);
}

function activePill(): string | null {
  const el = document.querySelector('.settings-nav-tab-active');
  return el ? el.textContent : null;
}

describe('SettingsRoute shell (#439)', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/settings');
    currentPath.value = '/settings';
  });

  it('renders General at /settings: appearance, category cards, diagnostics — no sections', () => {
    renderAt('/settings');
    expect(screen.getByText('Appearance')).toBeInTheDocument();
    expect(screen.getByText('Diagnostics')).toBeInTheDocument();
    // Category cards link to every sub-page.
    const cardTitles = Array.from(document.querySelectorAll('.settings-cat-title')).map(
      (el) => el.textContent,
    );
    expect(cardTitles).toEqual(['Account', 'Providers', 'Integrations', 'Workspace']);
    for (const id of ALL_SECTIONS) {
      expect(screen.queryByTestId(id)).not.toBeInTheDocument();
    }
    expect(activePill()).toBe('General');
  });

  it('renders GitHub + Mobile sections at /settings/account', () => {
    renderAt('/settings/account');
    expect(screen.getByTestId('sec-git')).toBeInTheDocument();
    expect(screen.getByTestId('sec-mobile')).toBeInTheDocument();
    expect(screen.queryByTestId('sec-messaging')).not.toBeInTheDocument();
    expect(activePill()).toBe('Account');
  });

  it('renders provider keys + MCP sections at /settings/providers', () => {
    renderAt('/settings/providers');
    expect(screen.getByTestId('sec-provider-keys')).toBeInTheDocument();
    expect(screen.getByTestId('sec-mcp')).toBeInTheDocument();
    expect(screen.queryByTestId('sec-git')).not.toBeInTheDocument();
    expect(activePill()).toBe('Providers');
  });

  it('renders the messaging section at /settings/integrations', () => {
    renderAt('/settings/integrations');
    expect(screen.getByTestId('sec-messaging')).toBeInTheDocument();
    expect(screen.queryByTestId('sec-updates')).not.toBeInTheDocument();
    expect(activePill()).toBe('Integrations');
  });

  it('renders updates + browser + metrics sections at /settings/workspace', () => {
    renderAt('/settings/workspace');
    expect(screen.getByTestId('sec-updates')).toBeInTheDocument();
    expect(screen.getByTestId('sec-browser')).toBeInTheDocument();
    expect(screen.getByTestId('sec-metrics')).toBeInTheDocument();
    expect(activePill()).toBe('Workspace');
  });

  it('falls back to General for an unknown sub-route', () => {
    renderAt('/settings/nope');
    expect(screen.getByText('Appearance')).toBeInTheDocument();
    for (const id of ALL_SECTIONS) {
      expect(screen.queryByTestId(id)).not.toBeInTheDocument();
    }
  });

  it('wraps sections in anchor ids for deep links', () => {
    renderAt('/settings/providers');
    expect(document.getElementById('mcp')).toContainElement(screen.getByTestId('sec-mcp'));
    expect(document.getElementById('providers')).toContainElement(
      screen.getByTestId('sec-provider-keys'),
    );
  });

  it('redirects legacy hash links: /settings#mcp → /settings/providers#mcp', () => {
    window.history.replaceState({}, '', '/settings#mcp');
    currentPath.value = '/settings';
    render(<SettingsRoute />);
    expect(currentPath.value).toBe('/settings/providers');
    expect(window.location.hash).toBe('#mcp');
  });

  it('navigates between groups when a pill is clicked', () => {
    renderAt('/settings');
    const pill = screen.getByRole('link', { name: 'Workspace' });
    pill.click();
    expect(currentPath.value).toBe('/settings/workspace');
  });

  it('keeps navigate() round-trips consistent with matchRoute', () => {
    navigate('/settings/providers');
    expect(currentPath.value).toBe('/settings/providers');
  });
});
