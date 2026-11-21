import { render, screen } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../../api/scans';
import type { ConnectionView } from '../../api/scans';
import { connection, connectionResult, modelSettingsOpen, targets } from '../../store/scans';
import { serverMode } from '../../store/server-mode';
import { ScanForm } from './ScanForm';

function view(state: ConnectionView['install']['state'], error = ''): ConnectionView {
  return {
    model: 'prov/model',
    api_base: '',
    has_key: true,
    uses_subscription: false,
    subscription: { signed_in: false },
    configured: true,
    install: { state, version: '1', error },
    backend: { ok: true, reason: '', detail: '' },
  };
}

const APP = { port: 3000, name: 'shop', addr: '0.0.0.0', reachable: true, reason: '' };

/** On a first connect, Save swaps the connect form for this one — so this
 *  is where the install and the connection check have to be said (#886). */
describe('ScanForm model status', () => {
  beforeEach(() => {
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
    vi.spyOn(api, 'listScanTargets').mockResolvedValue({ targets: [APP] });
    targets.value = [APP];
    connectionResult.value = null;
    modelSettingsOpen.value = false;
  });
  afterEach(() => vi.restoreAllMocks());

  it('says the scanner is being set up, and holds Start until it is', async () => {
    connection.value = view('installing');
    render(<ScanForm />);
    expect(await screen.findByText(/Setting up the scanner/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Start scan' })).toHaveProperty('disabled', true);
  });

  it('shows a failed connection check beside Start, with the way to fix it', async () => {
    connection.value = view('ready');
    connectionResult.value = { ok: false, detail: 'The scanner does not recognise this model name.' };
    render(<ScanForm />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('did not pass its connection check');
    expect(alert.textContent).toContain('does not recognise this model name');
    // Not blocked: a provider that was busy a minute ago may not be now.
    expect(screen.getByRole('button', { name: 'Start scan' })).toHaveProperty('disabled', false);
    await userEvent.click(screen.getByRole('button', { name: 'Model settings' }));
    expect(modelSettingsOpen.value).toBe(true);
  });

  it('says why the scanner could not be installed', async () => {
    connection.value = view('failed', 'pip exited 1');
    render(<ScanForm />);
    expect((await screen.findByRole('alert')).textContent).toContain('could not be set up: pip exited 1');
  });

  it('says nothing when the model is ready and passed', async () => {
    connection.value = view('ready');
    connectionResult.value = { ok: true, detail: 'The model answered.' };
    render(<ScanForm />);
    await screen.findByRole('button', { name: 'Start scan' });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText(/Setting up the scanner/)).toBeNull();
  });
});
