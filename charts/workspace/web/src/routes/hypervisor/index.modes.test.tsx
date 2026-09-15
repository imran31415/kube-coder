import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { HypervisorRoute } from './index';
import {
  closeThread,
  selectedProject,
  threads as threadStore,
} from '../../store/hypervisor';
import { _resetProjectsForTest } from '../../store/projects';
import type { HypervisorThread } from '../../api/hypervisor';

/**
 * One chat list for every mode (#683).
 *
 * Before this, Chat asked the server for `persona=default` and the AI CTO page
 * asked for `persona=cto`, so the two lists were disjoint: a CTO thread was
 * invisible to every thread-management affordance Chat has. This pins the
 * merged behaviour — CTO threads appear here, carry a read-only badge, and the
 * chip narrows the list without ever hiding anything by default.
 */

const MIXED: Partial<HypervisorThread>[] = [
  { id: '1', title: 'ship the chart', persona: '' },
  { id: '2', title: 'quarterly plan', persona: 'cto' },
  { id: '3', title: 'triage KC-14', persona: 'board' },
];

let threads = MIXED;
/** Query strings the SPA sent to the thread-list endpoint. */
let threadListUrls: string[] = [];

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
    threadListUrls.push(url);
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

beforeEach(() => {
  threads = MIXED;
  threadListUrls = [];
  localStorage.clear();
  _resetProjectsForTest();
  threadStore.value = [];
  selectedProject.value = '';
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
  _resetProjectsForTest();
});

function titles(container: Element): string[] {
  return [...container.querySelectorAll('.hv-thread-title')].map(
    (el) => el.textContent ?? '',
  );
}

describe('unified chat list (#683)', () => {
  it('asks the server for every thread, not just persona=default', async () => {
    render(<HypervisorRoute />);
    await screen.findByTitle('ship the chart');
    // The whole point: no persona scoping on the request.
    expect(threadListUrls.length).toBeGreaterThan(0);
    expect(threadListUrls.every((u) => !u.includes('persona='))).toBe(true);
  });

  it('shows CTO and board threads alongside plain chats', async () => {
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('ship the chart');
    expect(titles(container)).toEqual([
      'ship the chart',
      'quarterly plan',
      'triage KC-14',
    ]);
  });

  it('badges the threads that carry a persona, and only those', async () => {
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('quarterly plan');
    expect([...container.querySelectorAll('.hv-thread-mode')].map((el) => el.textContent)).toEqual([
      'CTO',
      'Board',
    ]);
  });

  it('narrows the list with the mode chips, defaulting to All', async () => {
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('ship the chart');

    fireEvent.click(screen.getByRole('button', { name: 'CTO' }));
    await waitFor(() => expect(titles(container)).toEqual(['quarterly plan']));

    // "Workspace" is "no persona" — a board thread is not a workspace chat.
    fireEvent.click(screen.getByRole('button', { name: 'Workspace' }));
    await waitFor(() => expect(titles(container)).toEqual(['ship the chart']));

    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    await waitFor(() => expect(titles(container)).toHaveLength(3));
  });

  it('hides the chip row in a workspace that has only plain chats', async () => {
    threads = [{ id: '1', title: 'ship the chart', persona: '' }];
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('ship the chart');
    await waitFor(() => expect(container.querySelector('.hv-modes')).toBeNull());
  });
});
