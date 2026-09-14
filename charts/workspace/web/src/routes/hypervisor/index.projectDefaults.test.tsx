import { render, screen, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { HypervisorRoute } from './index';
import {
  activeThreadId,
  closeThread,
  selectedAssistant,
  selectedEffort,
  selectedModel,
  selectedProject,
  threads as threadStore,
} from '../../store/hypervisor';
import { _resetProjectsForTest } from '../../store/projects';
import { navigate } from '../../store/router';
import type { Project } from '../../api/projects';
import type { HypervisorThread } from '../../api/hypervisor';

/**
 * Per-project assistant defaults (#483) and the project pulse (#466), now in
 * Chat (#683).
 *
 * Both were reachable only through the AI CTO page — one behind its gear, the
 * other on its projects rail. Chat already had the pickers; all it lacked was
 * the "make this the project's default" write, and the rail's pulse had no
 * home once the rail went away.
 */

const KC: Project = {
  id: 'kc',
  name: 'kube-coder',
  workdirs: [],
  repo: '',
  memory_namespace: 'project.kc',
  status: 'active',
  north_star: '',
  last_seen_at: 100,
  created_at: 0,
  updated_at: 0,
  pulse: { running: 2, waiting: 1, last_activity_at: 500 },
};

let projectList: Project[] = [KC];
let threads: Partial<HypervisorThread>[] = [];
/** Every PUT /api/projects/{id} body the SPA sent. */
let writes: unknown[] = [];

const NOW_SEC = Math.floor(Date.now() / 1000);

function payloadFor(url: string, init?: RequestInit): unknown {
  if (url.includes('/api/hypervisor/config')) {
    return {
      enabled: true,
      assistants: [
        { id: 'claude', label: 'Claude Code', models: ['default', 'opus'], efforts: ['low', 'high'], effort: 'high' },
        { id: 'ante', label: 'Ante CLI', models: [] },
      ],
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
  if (url.includes('/brief')) return null;
  if (/\/api\/projects\/[^/]+$/.test(url)) {
    if (init?.method === 'PUT') writes.push(JSON.parse(String(init.body)));
    return KC;
  }
  if (url.includes('/api/projects')) return { projects: projectList };
  if (url.includes('/api/workspace/dirs')) return { dirs: [] };
  return {};
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  projectList = [KC];
  threads = [];
  writes = [];
  localStorage.clear();
  _resetProjectsForTest();
  threadStore.value = [];
  selectedProject.value = '';
  selectedAssistant.value = 'claude';
  selectedModel.value = 'default';
  selectedEffort.value = 'high';
  navigate('/hypervisor');
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => payloadFor(String(url), init),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  localStorage.clear();
  closeThread();
  _resetProjectsForTest();
  navigate('/hypervisor');
});

async function openChat(id: string) {
  navigate(`/hypervisor/${id}`);
  await waitFor(() => expect(activeThreadId.value).toBe(id));
}

describe('per-project assistant defaults in Chat (#683)', () => {
  it('offers the write only when the chat is filed into a project', async () => {
    threads = [
      { id: 'a', title: 'filed', project_id: 'kc' },
      { id: 'b', title: 'unfiled', project_id: '' },
    ];
    render(<HypervisorRoute />);
    await screen.findByTitle('unfiled');

    await openChat('b');
    await waitFor(() =>
      expect(screen.queryByText('Set as project default')).toBeNull(),
    );

    await openChat('a');
    expect(await screen.findByText('Set as project default')).toBeTruthy();
  });

  it('writes the live selection onto the project record', async () => {
    threads = [{ id: 'a', title: 'filed', project_id: 'kc', assistant: 'claude', model: 'opus', effort: 'low' }];
    render(<HypervisorRoute />);
    await screen.findByTitle('filed');
    await openChat('a');

    (await screen.findByText('Set as project default')).click();

    await waitFor(() =>
      expect(writes).toEqual([
        { default_assistant: 'claude', default_model: 'opus', default_effort: 'low' },
      ]),
    );
  });

  it('says so instead of offering a write that would change nothing', async () => {
    projectList = [
      { ...KC, default_assistant: 'claude', default_model: 'opus', default_effort: 'low' },
    ];
    threads = [{ id: 'a', title: 'filed', project_id: 'kc', assistant: 'claude', model: 'opus', effort: 'low' }];
    render(<HypervisorRoute />);
    await screen.findByTitle('filed');
    await openChat('a');

    const btn = (await screen.findByText('Project default')) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(writes).toEqual([]);
  });

  it('seeds the next new chat from the project it will be filed into', async () => {
    // What the CTO page did when you clicked a project in its rail, applied to
    // the pickers a user actually has in front of them now.
    projectList = [
      { ...KC, default_assistant: 'ante', default_model: '', default_effort: '' },
    ];
    threads = [];
    render(<HypervisorRoute />);
    await screen.findByLabelText('Chat agent');
    selectedProject.value = 'kc';
    await waitFor(() => expect(selectedAssistant.value).toBe('ante'));
  });
});

describe('project pulse on the sidebar group headers (#683)', () => {
  it('shows the rail’s running/waiting dots and the unseen-activity marker', async () => {
    threads = [
      { id: 'a', title: 'filed', project_id: 'kc' },
      { id: 'b', title: 'unfiled', project_id: '' },
    ];
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('filed');

    const pulse = await waitFor(() => {
      const el = container.querySelector('.hv-group-pulse');
      expect(el).not.toBeNull();
      return el as Element;
    });
    expect(pulse.querySelector('.cto-dot-running')).not.toBeNull();
    expect(pulse.querySelector('.cto-dot-waiting')).not.toBeNull();
    // last_activity_at (500) is after last_seen_at (100) → unseen.
    expect(pulse.querySelector('.hv-group-unseen')).not.toBeNull();
    expect(pulse.textContent).toContain('2 running');
  });

  it('stays quiet for a project with nothing happening', async () => {
    projectList = [{ ...KC, pulse: { running: 0, waiting: 0, last_activity_at: 50 } }];
    threads = [{ id: 'a', title: 'filed', project_id: 'kc' }];
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('filed');
    await waitFor(() =>
      expect(container.querySelector('.hv-thread-group-head')).not.toBeNull(),
    );
    expect(container.querySelector('.hv-group-pulse')).toBeNull();
  });
});
