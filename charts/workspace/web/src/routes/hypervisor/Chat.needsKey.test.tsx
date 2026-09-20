import { render, screen, fireEvent, cleanup } from '@testing-library/preact';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { Chat } from './Chat';
import {
  events,
  activeThreadId,
  activeStatus,
  selectedAssistant,
  threads,
  config,
  sending,
  chatError,
} from '../../store/hypervisor';
import { claudeReady } from '../../store/claude';

/**
 * Starting a CHAT on an installed-but-unauthenticated agent (#702).
 *
 * The composer stays typeable on purpose — throwing away a half-written draft
 * because a key is missing would be its own bug — but Send is off and the
 * banner names the key. An already-open thread is exempt: it exists, and its
 * turns report their own auth errors through the normal error path.
 */

const DSH = {
  id: 'deepseek-harness',
  label: 'DeepSeek Harness',
  ready: false,
  needs: 'DEEPSEEK_API_KEY',
};

function setConfig(assistants: unknown[]) {
  config.value = {
    enabled: true,
    defaultAssistant: 'claude',
    workdir: '/',
    readOnly: false,
    assistants: assistants as never,
  };
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      headers: { get: () => 'application/json' },
      json: async () => ({}),
    })),
  );
  setConfig([{ id: 'claude', label: 'Claude Code' }, DSH]);
  activeThreadId.value = null;
  activeStatus.value = 'idle';
  sending.value = false;
  chatError.value = null;
  claudeReady.value = true;
  selectedAssistant.value = 'deepseek-harness';
  threads.value = [];
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

function composer() {
  return screen.getByPlaceholderText(/Message Kube-Coder/) as HTMLTextAreaElement;
}

function sendButton() {
  return screen.getByRole('button', { name: /Send/ }) as HTMLButtonElement;
}

describe('Chat — agent needs an API key (#702)', () => {
  it('names the missing key and links to provider settings', () => {
    render(<Chat />);
    const banner = screen.getByRole('alert');
    expect(banner.textContent).toContain('DeepSeek Harness needs an API key');
    expect(banner.textContent).toContain('DEEPSEEK_API_KEY');
    expect(banner.querySelector('a')!.getAttribute('href')).toContain(
      '/settings/providers',
    );
  });

  it('leaves the composer typeable but Send disabled', () => {
    render(<Chat />);
    const box = composer();
    expect(box.disabled).toBe(false);
    fireEvent.input(box, { target: { value: 'hello' } });
    expect(box.value).toBe('hello');
    expect(sendButton().disabled).toBe(true);
  });

  it('sends nothing when the form is submitted anyway', () => {
    render(<Chat />);
    fireEvent.input(composer(), { target: { value: 'hello' } });
    fireEvent.submit(composer().closest('form')!);
    // The draft survives — a refused send must not eat the message.
    expect(composer().value).toBe('hello');
    expect(sending.value).toBe(false);
  });

  it('says nothing and enables Send once the agent is ready', () => {
    setConfig([{ id: 'claude', label: 'Claude Code' }, { ...DSH, ready: true }]);
    render(<Chat />);
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.input(composer(), { target: { value: 'hello' } });
    expect(sendButton().disabled).toBe(false);
  });

  it('leaves an already-open thread alone', () => {
    // The thread exists; blocking it would strand a conversation whose own
    // turns already report auth failures.
    activeThreadId.value = 't1';
    threads.value = [
      {
        id: 't1',
        title: 'Test',
        assistant: 'deepseek-harness',
        status: 'idle',
        created_at: 1,
        updated_at: 1,
      },
    ];
    render(<Chat />);
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.input(composer(), { target: { value: 'hello' } });
    expect(sendButton().disabled).toBe(false);
  });
});
