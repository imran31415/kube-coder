import { render, screen } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { HypervisorRoute } from './index';
import {
  activeThreadId,
  closeThread,
  selectedProject,
  setChatContext,
  threads as threadStore,
} from '../../store/hypervisor';
import { _resetProjectsForTest } from '../../store/projects';
import { currentPath } from '../../store/router';
import type { HypervisorThread } from '../../api/hypervisor';

/**
 * Folder visibility (#637). A thread's folder is fixed at creation, but the
 * sidebar never showed it and the Folder picker kept displaying the new-chat
 * default while a chat was open — reading as "this chat's folder" and letting
 * chats get created in the wrong workdir unnoticed. These tests pin the fix:
 * each list item names its folder, and an open thread flips the picker to
 * that thread's folder, read-only.
 */

const THREADS: Partial<HypervisorThread>[] = [
  { id: '1', title: 'platform audit', workdir: '/home/dev/Umi' },
  { id: '2', title: 'nagme buildout', workdir: '/home/dev/Nagme' },
];

/** Recent enough to land under the sidebar's default "Active" tab. */
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
      threads: THREADS.map((t) => ({
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

beforeEach(() => {
  localStorage.clear();
  _resetProjectsForTest();
  threadStore.value = [];
  selectedProject.value = '';
  currentPath.value = '/hypervisor';
  globalThis.fetch = vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => payloadFor(String(url)),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  localStorage.clear();
  closeThread();
  setChatContext('', null);
  _resetProjectsForTest();
  selectedProject.value = '';
});

describe('per-thread folder visibility (#637)', () => {
  it('shows each chat’s folder, home-abbreviated, in the sidebar list', async () => {
    render(<HypervisorRoute />);
    const item = await screen.findByTitle('platform audit');
    expect(item.textContent).toContain('~/Umi');
    expect((await screen.findByTitle('nagme buildout')).textContent).toContain('~/Nagme');
  });

  it('keeps the new-chat folder picker editable when no chat is open', async () => {
    render(<HypervisorRoute />);
    const picker = (await screen.findByLabelText(
      'Folder for new chats',
    )) as HTMLInputElement;
    expect(picker.disabled).toBe(false);
  });

  it('flips to the open thread’s folder, read-only, when a chat is selected', async () => {
    // Deep-link the thread the way the app does — the URL effect owns
    // activeThreadId, so setting the signal directly would be fought.
    currentPath.value = '/hypervisor/1';
    activeThreadId.value = '1';
    render(<HypervisorRoute />);
    const shown = (await screen.findByLabelText(
      "This chat's folder",
    )) as HTMLInputElement;
    expect(shown.value).toBe('~/Umi');
    expect(shown.disabled).toBe(true);
    // The new-chat picker is gone — one control, one meaning at a time.
    expect(screen.queryByLabelText('Folder for new chats')).toBeNull();
  });
});
