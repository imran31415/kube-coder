import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type {
  ProviderSpec,
  CredentialsView,
  GatewayLink,
} from '../../api/gateway';

// ── canned catalog: Twilio + Meta, mirroring the #328 registry specs ──
const TWILIO: ProviderSpec = {
  id: 'twilio',
  display_name: 'Twilio',
  credential_fields: [
    { key: 'account_sid', label: 'Account SID', secret: false, placeholder: 'AC…', help_url: '', required: true },
    { key: 'auth_token', label: 'Auth Token', secret: true, placeholder: '', help_url: '', required: true },
  ],
  sender_field: { key: 'from_number', label: 'WhatsApp sender number', secret: false, placeholder: 'whatsapp:+1', help_url: '', required: true },
  capabilities: { proactive: false, max_text_len: 4096 },
};
const META: ProviderSpec = {
  id: 'meta',
  display_name: 'Meta (WhatsApp Cloud API)',
  credential_fields: [
    { key: 'access_token', label: 'Access Token', secret: true, placeholder: '', help_url: '', required: true },
    { key: 'app_secret', label: 'App Secret', secret: true, placeholder: '', help_url: '', required: true },
    { key: 'verify_token', label: 'Webhook Verify Token', secret: false, placeholder: '', help_url: '', required: true },
  ],
  sender_field: { key: 'phone_number_id', label: 'Phone Number ID', secret: false, placeholder: '123', help_url: '', required: true },
  capabilities: { proactive: true, max_text_len: 4096 },
};

let credView: CredentialsView = { configured: false, provider_id: null, fields: {} };
let linkList: GatewayLink[] = [];
const putSpy = vi.fn();
const delCredSpy = vi.fn();
const delLinkSpy = vi.fn();
let testResult = { ok: true, detail: 'HTTP 200' };

vi.mock('../../api/gateway', () => ({
  getProviders: () => Promise.resolve({ providers: [TWILIO, META], available: true }),
  getCredentials: () => Promise.resolve({ credentials: credView }),
  putCredentials: (b: unknown) => { putSpy(b); return Promise.resolve({ ok: true, credentials: credView }); },
  deleteCredentials: () => { delCredSpy(); return Promise.resolve({ ok: true }); },
  testConnection: () => Promise.resolve(testResult),
  createLink: () => Promise.resolve({ code: '483920', expires_in: 600, whatsapp_number: '', workspace: 'ws' }),
  listLinks: () => Promise.resolve({ links: linkList, available: true }),
  deleteLink: (id: string) => { delLinkSpy(id); return Promise.resolve({ ok: true }); },
}));

import { MessagingSection } from './MessagingSection';

describe('MessagingSection', () => {
  beforeEach(() => {
    credView = { configured: false, provider_id: null, fields: {} };
    linkList = [];
    testResult = { ok: true, detail: 'HTTP 200' };
    putSpy.mockClear();
    delCredSpy.mockClear();
    delLinkSpy.mockClear();
  });

  it('renders the default provider fields (data-driven)', async () => {
    render(<MessagingSection />);
    // Twilio is first → its fields show.
    expect(await screen.findByText('Account SID')).toBeTruthy();
    expect(screen.getByText('Auth Token')).toBeTruthy();
    expect(screen.getByText('WhatsApp sender number')).toBeTruthy();
  });

  it('switching to Meta renders exactly Meta\'s declared fields', async () => {
    render(<MessagingSection />);
    fireEvent.click(await screen.findByText('Meta (WhatsApp Cloud API)'));
    await waitFor(() => expect(screen.getByText('Access Token')).toBeTruthy());
    expect(screen.getByText('App Secret')).toBeTruthy();
    expect(screen.getByText('Webhook Verify Token')).toBeTruthy();
    expect(screen.getByText('Phone Number ID')).toBeTruthy();
    // Twilio-only field is gone.
    expect(screen.queryByText('Account SID')).toBeNull();
  });

  it('shows masked state and never leaks the secret', async () => {
    credView = {
      configured: true,
      provider_id: 'twilio',
      sender_field: 'from_number',
      fields: {
        account_sid: { set: true, value: 'AC1' },
        auth_token: { set: true, hint: '…9999' },
        from_number: { set: true, value: 'whatsapp:+14155238886' },
      },
    };
    render(<MessagingSection />);
    expect(await screen.findByText('set · …9999')).toBeTruthy();
    // The raw secret is never present.
    expect(document.body.innerHTML).not.toContain('super-secret');
    expect(screen.getByText('connected')).toBeTruthy();
  });

  it('Save calls putCredentials with provider_id and sender_number', async () => {
    render(<MessagingSection />);
    const sid = (await screen.findByPlaceholderText('AC…')) as HTMLInputElement;
    fireEvent.input(sid, { target: { value: 'AC123' } });
    const sender = screen.getByPlaceholderText('whatsapp:+1') as HTMLInputElement;
    fireEvent.input(sender, { target: { value: 'whatsapp:+19' } });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(putSpy).toHaveBeenCalled());
    const body = putSpy.mock.calls[0][0];
    expect(body.provider_id).toBe('twilio');
    expect(body.sender_number).toBe('whatsapp:+19');
    expect(body.creds.account_sid).toBe('AC123');
  });

  it('Test connection shows a pass pill', async () => {
    credView = { configured: true, provider_id: 'twilio', sender_field: 'from_number', fields: {} };
    render(<MessagingSection />);
    fireEvent.click(await screen.findByText('Test connection'));
    await waitFor(() => expect(screen.getByText(/connection ok/)).toBeTruthy());
  });

  it('Test connection shows a failure pill', async () => {
    credView = { configured: true, provider_id: 'twilio', sender_field: 'from_number', fields: {} };
    testResult = { ok: false, detail: 'authentication failed' };
    render(<MessagingSection />);
    fireEvent.click(await screen.findByText('Test connection'));
    await waitFor(() => expect(screen.getByText(/failed · authentication failed/)).toBeTruthy());
  });

  it('renders linked numbers and unlinks through the confirm', async () => {
    linkList = [{
      id: 'a'.repeat(64), channel: 'whatsapp', created_at: 0, updated_at: 0,
      bindings: [{ workspace: 'default', workspace_host: 'ws.example.com', is_default: true, has_thread: false, token_set: true, bound_at: 0 }],
    }];
    render(<MessagingSection />);
    expect(await screen.findByText('default')).toBeTruthy();
    fireEvent.click(screen.getByText('Unlink')); // opens the confirm dialog
    // The dialog renders via a Portal into document.body — query the document.
    const confirm = await waitFor(() => {
      const el = document.querySelector('button.cd-confirm') as HTMLButtonElement | null;
      if (!el) throw new Error('confirm not shown yet');
      return el;
    });
    fireEvent.click(confirm);
    await waitFor(() => expect(delLinkSpy).toHaveBeenCalledWith('a'.repeat(64)));
  });

  it('Disconnect clears credentials through the confirm', async () => {
    credView = { configured: true, provider_id: 'twilio', sender_field: 'from_number', fields: {} };
    render(<MessagingSection />);
    fireEvent.click(await screen.findByText('Disconnect'));
    const confirm = await waitFor(() => {
      const el = document.querySelector('button.cd-confirm') as HTMLButtonElement | null;
      if (!el) throw new Error('confirm not shown yet');
      return el;
    });
    fireEvent.click(confirm);
    await waitFor(() => expect(delCredSpy).toHaveBeenCalled());
  });
});
