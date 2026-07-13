import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

// Stub the network + URL helpers so the pane reaches phase==='ready'
// without a real backend.
vi.mock('../../api/tasks', () => ({
  prepareTerminal: vi.fn(() => Promise.resolve()),
  terminalUrl: () => '/oauth/terminal/?t=1',
  vncUrl: () => '/oauth/vnc-direct/vnc.html?t=1',
  openLocalhostPort: vi.fn(() => Promise.resolve({ ok: true })),
  getTaskOutput: vi.fn(() => Promise.resolve({ output: '' })),
}));
vi.mock('../../api/apps', () => ({
  proxyUrl: (port: number, suffix = '/') =>
    `/oauth/api/app-proxy/${port}${suffix.startsWith('/') ? suffix : `/${suffix}`}`,
}));

import { TerminalPane } from './TerminalPane';

beforeEach(() => {
  localStorage.clear();
});

describe('TerminalPane preview source toggle', () => {
  it('defaults to the in-app iframe, not the noVNC browser', async () => {
    render(<TerminalPane taskId="t1" withVnc />);
    const app = (await screen.findByTitle('App preview')) as HTMLIFrameElement;
    // Points at the default/persisted port through the reverse proxy.
    expect(app.getAttribute('src')).toContain('/api/app-proxy/8080/');
    expect(screen.queryByTitle('Workspace browser (VNC)')).toBeNull();
  });

  it('switches the right pane to noVNC when Browser is selected', async () => {
    render(<TerminalPane taskId="t1" withVnc />);
    await screen.findByTitle('App preview');

    fireEvent.click(screen.getByRole('button', { name: 'Browser' }));

    await waitFor(() => {
      expect(screen.getByTitle('Workspace browser (VNC)')).toBeInTheDocument();
    });
    expect(screen.queryByTitle('App preview')).toBeNull();
  });

  it('repoints the in-app iframe when a new port is opened', async () => {
    render(<TerminalPane taskId="t1" withVnc />);
    await screen.findByTitle('App preview');

    const portInput = screen.getByLabelText('Localhost port to preview') as HTMLInputElement;
    fireEvent.input(portInput, { target: { value: '3000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));

    await waitFor(() => {
      const f = screen.getByTitle('App preview') as HTMLIFrameElement;
      expect(f.getAttribute('src')).toContain('/api/app-proxy/3000/');
    });
  });

  it('remembers the chosen source across mounts', async () => {
    const first = render(<TerminalPane taskId="t1" withVnc />);
    await screen.findByTitle('App preview');
    fireEvent.click(screen.getByRole('button', { name: 'Browser' }));
    await screen.findByTitle('Workspace browser (VNC)');
    first.unmount();

    render(<TerminalPane taskId="t2" withVnc />);
    // Persisted 'browser' choice should win on the next open.
    await waitFor(() => {
      expect(screen.getByTitle('Workspace browser (VNC)')).toBeInTheDocument();
    });
    expect(screen.queryByTitle('App preview')).toBeNull();
  });
});

describe('TerminalPane copy hint (#243)', () => {
  it('surfaces the Shift-drag copy hint over the terminal on desktop', async () => {
    render(<TerminalPane taskId="t1" />);
    await screen.findByTitle('Task terminal');
    const hint = await screen.findByRole('note');
    expect(hint.textContent).toMatch(/Shift-drag/i);
  });

  it('dismisses permanently and stays gone across remounts', async () => {
    const first = render(<TerminalPane taskId="t1" />);
    await first.findByTitle('Task terminal');
    fireEvent.click(await first.findByRole('button', { name: 'Dismiss copy hint' }));
    await waitFor(() => expect(first.queryByRole('note')).toBeNull());
    // Persisted so a later mount never nags again.
    expect(localStorage.getItem('kc.term.copyHintDismissed')).toBe('1');
    first.unmount();

    render(<TerminalPane taskId="t2" />);
    await screen.findByTitle('Task terminal');
    expect(screen.queryByRole('note')).toBeNull();
  });
});

describe('TerminalPane mobile preview', () => {
  let savedMM: typeof window.matchMedia;
  beforeEach(() => {
    savedMM = window.matchMedia;
    window.matchMedia = ((q: string) => ({
      matches: /max-width:\s*720px/.test(q),
      media: q,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
    localStorage.clear();
  });
  afterEach(() => {
    window.matchMedia = savedMM;
  });

  it('shows one pane at a time with a Session|App selector (defaults to app)', async () => {
    render(<TerminalPane taskId="m1" withVnc />);
    // Defaults to the app pane; the terminal is not rendered alongside it.
    await screen.findByTitle('App preview');
    expect(screen.queryByTitle('Task terminal')).toBeNull();
    // The mobile-only Session pane button exists.
    const sessionBtn = screen.getByRole('button', { name: 'Session' });

    // Switching to Session swaps the single pane to the terminal.
    fireEvent.click(sessionBtn);
    await screen.findByTitle('Task terminal');
    expect(screen.queryByTitle('App preview')).toBeNull();
    // The Shift-drag copy hint is desktop-only (touch has separate copy
    // limits), so it must not appear on the mobile Session pane.
    expect(screen.queryByRole('note')).toBeNull();
  });
});
