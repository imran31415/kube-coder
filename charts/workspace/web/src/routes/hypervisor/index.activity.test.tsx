import { act, cleanup, fireEvent, render, screen } from '@testing-library/preact';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { HypervisorRoute } from './index';
import { closeThread, threads, selectedProject } from '../../store/hypervisor';
import { _resetProjectsForTest } from '../../store/projects';
import { currentPath } from '../../store/router';
import type { HypervisorThread } from '../../api/hypervisor';

const NOW = 1_800_000_000_000;
let serverThreads: HypervisorThread[];
let listCalls: number;
let failList: boolean;
const originalFetch = globalThis.fetch;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  localStorage.clear();
  _resetProjectsForTest();
  threads.value = [];
  selectedProject.value = '';
  currentPath.value = '/hypervisor/other';
  listCalls = 0;
  failList = false;
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  serverThreads = [
    { id: 'other', title: 'Current chat', assistant: 'codex', status: 'idle', created_at: NOW / 1000, updated_at: NOW / 1000 },
    { id: 'old', title: 'Old CTO', assistant: 'codex', persona: 'cto', project_id: 'p', status: 'idle', created_at: NOW / 1000 - 172800, updated_at: NOW / 1000 - 172800 },
  ];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input).split('?')[0];
    let payload: unknown = {};
    if (path.endsWith('/api/hypervisor/config')) payload = {
      enabled: true, defaultAssistant: 'codex', workdir: '/tmp',
      assistants: [{ id: 'codex', label: 'Codex' }],
    };
    else if (path.endsWith('/api/hypervisor/threads')) {
      listCalls++;
      if (failList) throw new Error('Offline');
      payload = { threads: [...serverThreads] };
    } else if (path.includes('/api/hypervisor/threads/')) payload = {
      thread: serverThreads.find(t => t.id === path.split('/').pop()), events: [], source: 'capture',
    };
    else if (path.endsWith('/api/projects')) payload = { projects: [{ id: 'p', name: 'Test project' }] };
    else if (path.endsWith('/api/workspace/dirs')) payload = { dirs: [] };
    return new Response(JSON.stringify(payload), { headers: { 'Content-Type': 'application/json' } });
  }) as typeof fetch;
});

afterEach(() => {
  cleanup();
  closeThread();
  _resetProjectsForTest();
  globalThis.fetch = originalFetch;
  vi.useRealTimers();
  localStorage.clear();
});

async function tick(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

function resumeCto() {
  serverThreads = serverThreads.map(t => t.id === 'old' ? { ...t, status: 'running', updated_at: NOW / 1000 } : t);
}

it('discovers background CTO activity, updates counts/groups, and preserves filters', async () => {
  render(<HypervisorRoute />);
  await tick();
  expect(listCalls).toBe(1);
  expect(screen.queryByTitle('Old CTO')).toBeNull();
  resumeCto();
  await tick(5100);
  expect(screen.getByTitle('Old CTO')).toBeInTheDocument();
  expect(screen.getByRole('tab', { name: /Active/ })).toHaveTextContent('2');
  expect(screen.getByText('Test project', { selector: '.hv-thread-group-name' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'CTO', exact: true }));
  await tick();
  expect(screen.queryByTitle('Current chat')).toBeNull();
  await tick(5000);
  expect(screen.queryByTitle('Current chat')).toBeNull();
  expect(screen.getByTitle('Old CTO')).toBeInTheDocument();
});

it('retains rows through failure, backs off, then recovers', async () => {
  render(<HypervisorRoute />);
  await tick();
  failList = true;
  await tick(5100);
  expect(listCalls).toBe(2);
  expect(screen.getByTitle('Current chat')).toBeInTheDocument();
  await tick(5000);
  expect(listCalls).toBe(2);
  failList = false;
  resumeCto();
  await tick(2500);
  expect(listCalls).toBe(3);
  expect(screen.getByTitle('Old CTO')).toBeInTheDocument();
});

it('pauses the list while hidden, resumes when visible, and stops on unmount', async () => {
  const { unmount } = render(<HypervisorRoute />);
  await tick();
  Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
  await tick(10000);
  expect(listCalls).toBe(1);
  resumeCto();
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
  await tick();
  expect(screen.getByTitle('Old CTO')).toBeInTheDocument();
  const calls = listCalls;
  unmount();
  await tick(10000);
  expect(listCalls).toBe(calls);
});

it('keeps the chosen Past tab and the open old chat visible as activity changes', async () => {
  render(<HypervisorRoute />);
  await tick();
  fireEvent.click(screen.getByRole('tab', { name: /Past/ }));
  await tick();
  fireEvent.click(screen.getByTitle('Old CTO'));
  await tick();
  expect(screen.getByTitle('Old CTO')).toBeInTheDocument();
  resumeCto();
  await tick(5100);
  expect(screen.getByRole('tab', { name: /Past/ })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByTitle('Old CTO')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: /Active/ }));
  await tick();
  expect(screen.getByTitle('Old CTO')).toBeInTheDocument();
});
