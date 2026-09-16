import { render, screen, fireEvent, cleanup } from '@testing-library/preact';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { Chat } from './Chat';
import { events, activeThreadId, activeStatus, selectedAssistant, threads, config, sending, chatError } from '../../store/hypervisor';
import { claudeReady } from '../../store/claude';

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, headers: { get: () => 'application/json' }, json: async () => ({}) })));
  config.value = { enabled: true, defaultAssistant: 'claude', workdir: '/', readOnly: false, assistants: [] };
  activeThreadId.value = 'codex-thread';
  activeStatus.value = 'idle';
  sending.value = false;
  chatError.value = null;
  claudeReady.value = true;
  selectedAssistant.value = 'antigravity';
  threads.value = [{ id: 'codex-thread', title: 'Test', assistant: 'codex', status: 'idle', created_at: 1, updated_at: 1 }];
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  events.value = [];
  threads.value = [];
  activeThreadId.value = null;
  selectedAssistant.value = '';
  config.value = null;
});

describe('agent identity and authentication guidance', () => {
  it('shows one recovery link for repeated authentication errors in a turn', () => {
    events.value = [
      { seq: 1, ts: 1, role: 'system', type: 'error', auth_required: true, text: '401 Unauthorized' },
      { seq: 2, ts: 2, role: 'system', type: 'error', auth_required: true, text: 'Token expired' },
    ];
    render(<Chat />);
    expect(screen.getAllByRole('link', { name: 'Provider settings' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Authentication required' }));
    expect(screen.getByText(/401 Unauthorized\s+Token expired/)).toBeTruthy();
  });

  it('offers setup for an unavailable provider without calling it an auth failure', () => {
    events.value = [{ seq: 1, ts: 1, role: 'system', type: 'error', setup_required: true, text: 'Ante provider unavailable' }];
    render(<Chat />);
    expect(screen.getByRole('button', { name: 'Agent setup required' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Provider settings' })).toBeTruthy();
  });
  it('keeps the prompt when Claude authentication blocks a send', () => {
    activeThreadId.value = null;
    selectedAssistant.value = 'claude';
    events.value = [];
    claudeReady.value = false;
    const { container } = render(<Chat />);
    const composer = screen.getByLabelText('Message Kube-Coder') as HTMLTextAreaElement;
    fireEvent.input(composer, { target: { value: 'Keep this prompt' } });
    fireEvent.submit(container.querySelector('form')!);
    expect(composer.value).toBe('Keep this prompt');
    expect(events.value).toEqual([]);
    expect(screen.getByRole('link', { name: 'Provider settings' }).getAttribute('target')).toBe('_blank');
  });

  it('puts a failed prompt back into the composer without sending it', () => {
    events.value = [
      { seq: 1, ts: 1, role: 'user', type: 'message', text: 'Try this again' },
      { seq: 2, ts: 2, role: 'system', type: 'error', auth_required: true, text: '401 Unauthorized' },
    ];
    render(<Chat />);
    fireEvent.click(screen.getByRole('button', { name: 'Use prompt again' }));
    expect((screen.getByLabelText('Message Kube-Coder') as HTMLTextAreaElement).value).toBe('Try this again');
    expect(events.value).toHaveLength(2);
  });

  it('keeps Codex attribution when the new-chat agent changes', () => {
    events.value = [{ seq: 1, ts: 1, role: 'system', type: 'error', text: 'codex exited with code 2' }];
    render(<Chat />);
    expect(screen.getByText('via codex')).toBeTruthy();
    expect(screen.queryByText('via antigravity')).toBeNull();
    expect(screen.queryByRole('link', { name: 'Provider settings' })).toBeNull();
  });

  it('shows a setup link immediately and keeps diagnostics expandable', () => {
    events.value = [{ seq: 1, ts: 1, role: 'system', type: 'error', auth_required: true, text: '401 Unauthorized: Missing bearer' }];
    render(<Chat />);
    expect(screen.getByRole('link', { name: 'Provider settings' }).getAttribute('href')).toContain('/settings/providers#providers');
    expect(screen.queryByText('401 Unauthorized: Missing bearer')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Authentication required' }));
    expect(screen.getByText('401 Unauthorized: Missing bearer')).toBeTruthy();
  });
});
