import { render, screen, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { HypervisorRoute } from './index';
import {
  activeThreadId,
  closeThread,
  threads as threadStore,
} from '../../store/hypervisor';
import { _resetProjectsForTest, selectedProjectId } from '../../store/projects';
import { navigate } from '../../store/router';
import type { HypervisorThread } from '../../api/hypervisor';

/**
 * The project brief as a pane of Chat (#683).
 *
 * The brief, the dev container card and the project pulse were trapped on the
 * /cto page — useful for exactly one route. They are deterministic (the server
 * aggregates them; zero LLM calls), so they are useful to ANY chat filed into a
 * project, which is the rule this pins: brief ⇔ the open chat has a project,
 * regardless of the chat's mode.
 */

const BRIEF = {
  project: { id: 'kc', name: 'kube-coder', repo: 'imran31415/kube-coder', north_star: 'Ship it' },
  goals: [],
  decisions: [],
  memories: [],
  tasks: { total: 0, waiting: 0, recent: [] },
  counts: { memories: 0 },
  git: [],
  devcontainer: [],
};

let threads: Partial<HypervisorThread>[] = [];
/** Every URL the SPA fetched, so we can assert what a bare chat does NOT do. */
let urls: string[] = [];

const NOW_SEC = Math.floor(Date.now() / 1000);

function payloadFor(url: string): unknown {
  urls.push(url);
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
  if (url.includes('/brief')) return BRIEF;
  if (url.includes('/api/projects')) {
    return { projects: [{ id: 'kc', name: 'kube-coder', memory_namespace: 'project.kc', workdirs: [] }] };
  }
  if (url.includes('/api/workspace/dirs')) return { dirs: [] };
  return {};
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  threads = [];
  urls = [];
  localStorage.clear();
  _resetProjectsForTest();
  threadStore.value = [];
  navigate('/hypervisor');
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
  navigate('/hypervisor');
});

/** happy-dom's viewport is 1024px wide, which is inside the auto-collapse band
 *  (the chat would be uncomfortably narrow between three columns). Tests that
 *  care about the panel's CONTENT record the explicit "expanded" choice a user
 *  would have made; the auto-collapse default has its own test below. */
function expandBrief() {
  localStorage.setItem('kc.hvBriefCollapsed', '0');
}

/** Open a chat the way the route does — through the URL. */
async function openChat(id: string) {
  navigate(`/hypervisor/${id}`);
  await waitFor(() => expect(activeThreadId.value).toBe(id));
}

describe('brief pane in Chat (#683)', () => {
  it('shows the brief for a project-bound chat', async () => {
    expandBrief();
    threads = [{ id: 'a', title: 'ship the chart', project_id: 'kc' }];
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('ship the chart');
    await openChat('a');
    // The project name renders in the sidebar's group header too, so assert
    // on the brief's own title rather than on the string.
    await waitFor(() =>
      expect(container.querySelector('.cto-brief-title')?.textContent).toBe('kube-coder'),
    );
    expect(container.querySelector('.cto-brief-repo')?.textContent).toBe(
      'imran31415/kube-coder',
    );
  });

  it('shows it for a plain workspace chat too, not just CTO ones', async () => {
    // Decided scope: the brief belongs to any project-bound chat. Gating it on
    // the mode would have left it as page-shaped as it was before.
    expandBrief();
    threads = [{ id: 'a', title: 'plain chat', project_id: 'kc', persona: '' }];
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('plain chat');
    await openChat('a');
    await waitFor(() => expect(container.querySelector('.cto-brief')).not.toBeNull());
  });

  it('leaves an unfiled chat with the two-column layout, and fetches no brief', async () => {
    threads = [{ id: 'b', title: 'random question', project_id: '' }];
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('random question');
    await openChat('b');
    await waitFor(() =>
      expect(container.querySelector('.route-hypervisor')?.getAttribute('style')).toContain(
        'minmax(0, 1fr)',
      ),
    );
    expect(container.querySelector('.cto-brief')).toBeNull();
    expect(container.querySelector('.cto-brief-tab')).toBeNull();
    expect(urls.some((u) => u.includes('/brief'))).toBe(false);
  });

  it('points the projects store at the open chat’s project', async () => {
    // This is also what stamps last_seen_at now that the CTO rail is going
    // away: "you opened a chat filed under this project" replaces "you looked
    // at this project in the rail".
    threads = [{ id: 'a', title: 'ship the chart', project_id: 'kc' }];
    render(<HypervisorRoute />);
    await screen.findByTitle('ship the chart');
    await openChat('a');
    await waitFor(() => expect(selectedProjectId.value).toBe('kc'));
  });

  it('folds to the edge tab by default when the viewport is cramped', async () => {
    // No stored choice: the width heuristic decides, and 1024px is inside the
    // band where a third column would squeeze the chat.
    threads = [{ id: 'a', title: 'ship the chart', project_id: 'kc' }];
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('ship the chart');
    await openChat('a');
    await waitFor(() => expect(container.querySelector('.cto-brief-tab')).not.toBeNull());
  });

  it('honours a persisted collapse choice, showing the edge tab instead', async () => {
    localStorage.setItem('kc.hvBriefCollapsed', '1');
    threads = [{ id: 'a', title: 'ship the chart', project_id: 'kc' }];
    const { container } = render(<HypervisorRoute />);
    await screen.findByTitle('ship the chart');
    await openChat('a');
    await waitFor(() => expect(container.querySelector('.cto-brief-tab')).not.toBeNull());
    expect(container.querySelector('.cto-brief')).toBeNull();
  });
});
