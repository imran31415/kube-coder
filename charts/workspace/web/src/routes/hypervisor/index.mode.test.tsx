import { render, screen, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { HypervisorRoute } from './index';
import {
  closeThread,
  newChatMode,
  threads as threadStore,
} from '../../store/hypervisor';
import { _resetProjectsForTest } from '../../store/projects';
import { serverMode } from '../../store/server-mode';
import type { HypervisorThread } from '../../api/hypervisor';

/**
 * The Mode picker in Chat's sidebar (#683), rendered for real.
 *
 * Mode is the whole reason the AI CTO needed its own page: a different
 * preamble. Since the preamble is delivered once, on turn 1, the control is a
 * new-chat default — and with a chat open it shows that chat's mode read-only,
 * the same two-mode shape the Folder picker got in #637.
 */

/**
 * Pick an option and let the component hear about it.
 *
 * Not `fireEvent.change`: @testing-library/preact rewrites that to an `input`
 * event for the whole process the moment it sees a single preact/compat vnode,
 * and the Chat subtree rendered by this route contains one. Preact registers
 * `onChange` as a real `change` listener, so the rewritten event silently never
 * reaches the handler and the assertion fails for a reason that has nothing to
 * do with the component.
 */
function selectOption(el: HTMLElement, value: string): void {
  (el as HTMLSelectElement).value = value;
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

let threads: Partial<HypervisorThread>[] = [];
const NOW_SEC = Math.floor(Date.now() / 1000);

function payloadFor(url: string): unknown {
  if (url.includes('/api/hypervisor/config')) {
    return {
      enabled: true,
      assistants: [{ id: 'claude', label: 'Claude Code' }],
      defaultAssistant: 'claude',
      workdir: '/home/dev',
    };
  }
  if (url.includes('/api/hypervisor/threads')) {
    return {
      threads: threads.map((t) => ({
        assistant: 'claude',
        status: 'idle',
        created_at: NOW_SEC,
        updated_at: NOW_SEC,
        ...t,
      })),
    };
  }
  if (url.includes('/api/projects')) return { projects: [] };
  if (url.includes('/api/workspace/dirs')) return { dirs: [] };
  return {};
}

const realFetch = globalThis.fetch;
const realMode = serverMode.value;

beforeEach(() => {
  threads = [];
  localStorage.clear();
  _resetProjectsForTest();
  threadStore.value = [];
  newChatMode.value = '';
  serverMode.value = { ...realMode, readOnly: false, ctoEnabled: true };
  globalThis.fetch = vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => payloadFor(String(url)),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  serverMode.value = realMode;
  localStorage.clear();
  closeThread();
  newChatMode.value = '';
  _resetProjectsForTest();
});

describe('Mode picker (#683)', () => {
  it('offers Workspace and CTO for the next new chat', async () => {
    render(<HypervisorRoute />);
    const picker = (await screen.findByLabelText(
      'Mode for new chats',
    )) as HTMLSelectElement;
    expect([...picker.options].map((o) => o.textContent)).toEqual([
      'Workspace',
      'CTO',
    ]);
    expect(picker.value).toBe('');
  });

  it('picking CTO sets what the next new chat is created as', async () => {
    render(<HypervisorRoute />);
    const picker = await screen.findByLabelText('Mode for new chats');
    selectOption(picker, 'cto');
    await waitFor(() => expect(newChatMode.value).toBe('cto'));
  });

  it('tells the user a CTO chat starts in its project folder', async () => {
    // The folder picker has nothing to say in CTO mode — the server resolves
    // the folder from the project record, so showing one would lie.
    render(<HypervisorRoute />);
    selectOption(await screen.findByLabelText('Mode for new chats'), 'cto');
    await waitFor(() => {
      const folder = screen.getByLabelText('Folder for new chats') as HTMLInputElement;
      expect(folder.disabled).toBe(true);
      expect(folder.value).toBe('Project folder');
    });
  });

  it('hides the picker entirely when the workspace does not offer CTO mode', async () => {
    serverMode.value = { ...serverMode.value, ctoEnabled: false };
    render(<HypervisorRoute />);
    await screen.findByLabelText('Chat agent');
    await waitFor(() =>
      expect(screen.queryByLabelText('Mode for new chats')).toBeNull(),
    );
  });

  it('clears a stale CTO selection when the capability is off', async () => {
    newChatMode.value = 'cto';
    serverMode.value = { ...serverMode.value, ctoEnabled: false };
    render(<HypervisorRoute />);
    await waitFor(() => expect(newChatMode.value).toBe(''));
  });
});
