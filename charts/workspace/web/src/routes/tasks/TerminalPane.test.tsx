import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, vi, beforeEach } from 'vitest';

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
