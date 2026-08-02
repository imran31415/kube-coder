import { render, screen, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { HypervisorRoute } from './index';
import {
  closeThread,
  selectedProject,
  setChatContext,
  threads as threadStore,
} from '../../store/hypervisor';
import { _resetProjectsForTest } from '../../store/projects';
import type { HypervisorThread } from '../../api/hypervisor';

/**
 * The Chat sidebar's project grouping and the topbar's project picker (#358),
 * rendered for real — a passing helper unit test says nothing about whether the
 * sidebar actually shows the grouping.
 */

const THREADS: Partial<HypervisorThread>[] = [
  { id: '1', title: 'ship the chart', project_id: 'kc' },
  { id: '2', title: 'fix the rack', project_id: 'pool' },
  { id: '3', title: 'random question', project_id: '' },
];

let threads = THREADS;

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
      threads: threads.map((t) => ({
        assistant: 'claude',
        status: 'idle',
        created_at: NOW_SEC,
        updated_at: NOW_SEC,
        ...t,
      })),
    };
  }
  if (url.includes('/api/projects')) {
    return {
      projects: [
        { id: 'kc', name: 'kube-coder', memory_namespace: 'project.kc', workdirs: [] },
        { id: 'pool', name: 'Pool Hall', memory_namespace: 'project.pool', workdirs: [] },
      ],
    };
  }
  if (url.includes('/api/workspace/dirs')) return { dirs: [] };
  return {};
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  threads = THREADS;
  localStorage.clear();
  _resetProjectsForTest();
  // The store is module-level state: a stale list from the previous test would
  // satisfy the waitFor before this test's own fetch lands.
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
  setChatContext('', null);
  _resetProjectsForTest();
  selectedProject.value = '';
});

/** The sidebar's group headers, in render order (the picker's options share
 *  some of the same text, so match the header element, not the string). */
function groupHeaders(container: Element): string[] {
  return [...container.querySelectorAll('.hv-thread-group-name')].map(
    (el) => el.textContent ?? '',
  );
}

describe('chat list grouped by project (#358)', () => {
  it('renders a header per project plus a trailing "No project" group', async () => {
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('ship the chart');
    expect(groupHeaders(container)).toEqual(['kube-coder', 'Pool Hall', 'No project']);
    // Every chat still renders, each under its own group.
    expect(screen.getByTitle('fix the rack')).toBeTruthy();
    expect(screen.getByTitle('random question')).toBeTruthy();
  });

  it('shows no group headers at all when nothing is filed', async () => {
    threads = [{ id: '3', title: 'random question', project_id: '' }];
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('random question');
    expect(groupHeaders(container)).toEqual([]);
  });

  it('offers a project picker seeded from the registry', async () => {
    render(<HypervisorRoute />);
    const picker = (await screen.findByLabelText(
      'Project for this chat',
    )) as HTMLSelectElement;
    expect([...picker.options].map((o) => o.textContent)).toEqual([
      'No project',
      'kube-coder',
      'Pool Hall',
    ]);
    // No chat open → the picker reflects what the NEXT new chat is filed into.
    expect(picker.value).toBe('');
  });

  it('hides the picker entirely in a workspace with no projects', async () => {
    globalThis.fetch = vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () =>
        String(url).includes('/api/projects')
          ? { projects: [] }
          : payloadFor(String(url)),
    })) as unknown as typeof fetch;
    render(<HypervisorRoute />);
    await screen.findByTitle('random question');
    await waitFor(() =>
      expect(screen.queryByLabelText('Project for this chat')).toBeNull(),
    );
  });
});
