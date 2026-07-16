import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { AppEntry } from '../../api/apps';
import type { TaskSummary } from '../../api/tasks';

// Keep the real signals but neuter the pollers so the effect doesn't hit the
// network during the test.
vi.mock('../../store/metrics', async (orig) => ({
  ...(await orig<typeof import('../../store/metrics')>()),
  startMetricsPolling: vi.fn(),
  refreshMetrics: vi.fn(),
}));

const killed: string[] = [];
const opened: string[] = [];
vi.mock('../../store/tasks', async (orig) => ({
  ...(await orig<typeof import('../../store/tasks')>()),
  startTaskPolling: vi.fn(),
  killTask: (id: string) => {
    killed.push(id);
    return Promise.resolve();
  },
  selectTask: (id: string | null) => {
    if (id) opened.push(id);
  },
}));

let apps: AppEntry[] = [];
vi.mock('../../api/apps', async (orig) => ({
  ...(await orig<typeof import('../../api/apps')>()),
  listApps: () => Promise.resolve({ apps, unavailable_reason: null, auth_mode: 'oauth2' }),
}));

import { MetricsSection } from './MetricsSection';
import { metrics, health } from '../../store/metrics';
import { tasks } from '../../store/tasks';
import { serverMode } from '../../store/server-mode';

function task(over: Partial<TaskSummary>): TaskSummary {
  return {
    task_id: 't1',
    name: null,
    prompt: 'do a thing',
    status: 'running',
    created_at: 0,
    finished_at: null,
    source: 'cli',
    kind: 'claude',
    ...over,
  };
}

describe('MetricsSection', () => {
  beforeEach(() => {
    killed.length = 0;
    opened.length = 0;
    apps = [];
    metrics.value = null;
    health.value = null;
    tasks.value = [];
    // Writable deploy so MutatorOnly renders the Stop button.
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  });

  it('lists running builds/chats and hides finished ones', async () => {
    tasks.value = [
      task({ task_id: 'run-1', name: 'Build the app', status: 'running' }),
      task({ task_id: 'wait-1', prompt: 'Awaiting review', status: 'waiting-for-input' }),
      task({ task_id: 'done-1', name: 'Old task', status: 'completed' }),
    ];
    render(<MetricsSection />);
    expect(await screen.findByText('Build the app')).toBeInTheDocument();
    expect(screen.getByText('Awaiting review')).toBeInTheDocument();
    expect(screen.getByText('waiting')).toBeInTheDocument();
    expect(screen.queryByText('Old task')).not.toBeInTheDocument();
  });

  it('opens a running task via selectTask', async () => {
    tasks.value = [task({ task_id: 'run-1', name: 'Build the app' })];
    render(<MetricsSection />);
    const openBtn = (await screen.findAllByText('Open'))[0];
    fireEvent.click(openBtn);
    expect(opened).toContain('run-1');
  });

  it('confirms before killing a running task', async () => {
    tasks.value = [task({ task_id: 'run-1', name: 'Build the app' })];
    render(<MetricsSection />);
    await screen.findByText('Build the app');
    fireEvent.click(screen.getByLabelText('Stop this session'));
    // ConfirmDialog appears; the kill only fires on confirm.
    expect(killed).toHaveLength(0);
    fireEvent.click(screen.getByText('Stop'));
    await waitFor(() => expect(killed).toContain('run-1'));
  });

  it('lists only listening ports and shows an empty state otherwise', async () => {
    apps = [
      { port: 3000, name: 'Dev server', pinned: true, status: 'running', strip_prefix: false, addr: '127.0.0.1' },
      { port: 8081, name: '', pinned: false, status: 'stopped', strip_prefix: false, addr: '0.0.0.0' },
    ];
    render(<MetricsSection />);
    expect(await screen.findByText('Dev server')).toBeInTheDocument();
    expect(screen.getByText(':3000')).toBeInTheDocument();
    // The 'stopped' pin is filtered out.
    expect(screen.queryByText(':8081')).not.toBeInTheDocument();
    expect(screen.getByText('No builds or chats are running right now.')).toBeInTheDocument();
  });
});
