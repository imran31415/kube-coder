import { render, screen } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../../api/scans';
import type { ConnectionView } from '../../api/scans';
import { connection, connectionResult } from '../../store/scans';
import { serverMode } from '../../store/server-mode';
import { ConnectStrix } from './ConnectStrix';

function view(state: ConnectionView['install']['state']): ConnectionView {
  return {
    model: 'prov/model',
    api_base: '',
    has_key: true,
    uses_subscription: false,
    subscription: { signed_in: false },
    configured: true,
    install: { state, version: '1', error: '' },
    backend: { ok: true, reason: '', detail: '' },
  };
}

describe('ConnectStrix', () => {
  beforeEach(() => {
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
    connection.value = null;
    connectionResult.value = null;
  });
  afterEach(() => vi.restoreAllMocks());

  it('never lets the browser fill a saved login into it (#886)', async () => {
    // A text box above a password box reads to a password manager as a
    // sign-in form, so a saved Account SID and Auth Token landed in Model
    // and API key. `new-password` is the hint browsers do not fill.
    vi.spyOn(api, 'getScanConnection').mockResolvedValue(view('ready'));
    render(<ConnectStrix />);
    const key = screen.getByPlaceholderText(/Saved — type to replace|Paste your key/);
    expect(key.getAttribute('type')).toBe('password');
    expect(key.getAttribute('autocomplete')).toBe('new-password');
    expect(screen.getByPlaceholderText('provider/model-name').getAttribute('autocomplete')).toBe('off');
    expect(screen.getByPlaceholderText('http://localhost:11434').getAttribute('autocomplete')).toBe('off');
  });

  it('does not run the check mid-install, when it could only say "still installing"', async () => {
    vi.spyOn(api, 'getScanConnection').mockResolvedValue(view('ready'));
    vi.spyOn(api, 'saveScanConnection').mockResolvedValue(view('installing'));
    const check = vi.spyOn(api, 'testScanConnection').mockResolvedValue({ ok: true, detail: '' });
    render(<ConnectStrix />);
    await userEvent.type(screen.getByPlaceholderText('provider/model-name'), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await vi.waitFor(() => expect(connection.value?.install.state).toBe('installing'));
    expect(check).not.toHaveBeenCalled();
  });

  it('runs the check straight after a save once the scanner is installed', async () => {
    vi.spyOn(api, 'getScanConnection').mockResolvedValue(view('ready'));
    vi.spyOn(api, 'saveScanConnection').mockResolvedValue(view('ready'));
    const check = vi.spyOn(api, 'testScanConnection').mockResolvedValue({ ok: true, detail: '' });
    render(<ConnectStrix />);
    await userEvent.type(screen.getByPlaceholderText('provider/model-name'), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await vi.waitFor(() => expect(check).toHaveBeenCalledTimes(1));
  });
});
