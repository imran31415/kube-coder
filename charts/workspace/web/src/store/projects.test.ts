import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import {
  projects,
  selectedProjectId,
  brief,
  refreshProjects,
  selectProject,
  mostActive,
  activeProject,
  defaultMemoryNamespace,
  USER_NAMESPACE,
  _onDashboardEventForTest as onEvent,
  _resetProjectsForTest,
} from './projects';
import type { Project, ProjectBrief } from '../api/projects';

const now = Math.floor(Date.now() / 1000);

function project(over: Partial<Project>): Project {
  return {
    id: 'p', name: 'P', workdirs: ['/home/dev/p'], repo: '', memory_namespace: 'project.p',
    status: 'active', north_star: '', last_seen_at: null,
    created_at: now, updated_at: now,
    pulse: { running: 0, waiting: 0, last_activity_at: null },
    ...over,
  };
}

function briefFor(id: string): ProjectBrief {
  return {
    project: project({ id, name: id }),
    tasks: { running: 1, waiting: 2, total: 3, recent: [] },
    goals: [], decisions: [], memories: [], git: [], triggers: [],
    counts: { goals: 0, decisions: 0, memories: 0, tasks: 3 },
    brief_markdown: `# ${id}`,
  };
}

/** Mock fetch keyed by URL so listProjects/getProjectBrief/updateProject resolve. */
function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => ({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => handler(String(url), init),
  })) as unknown as typeof fetch;
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  _resetProjectsForTest();
});
afterEach(() => {
  _resetProjectsForTest();
  globalThis.fetch = realFetch;
});

describe('store/projects', () => {
  it('mostActive picks the highest last_activity_at', () => {
    const a = project({ id: 'a', pulse: { running: 0, waiting: 0, last_activity_at: 100 } });
    const b = project({ id: 'b', pulse: { running: 0, waiting: 0, last_activity_at: 999 } });
    expect(mostActive([a, b])?.id).toBe('b');
    expect(mostActive([])).toBeNull();
  });

  it('refreshProjects loads the registry', async () => {
    mockFetch(() => ({ projects: [project({ id: 'kc', name: 'kube-coder' })] }));
    await refreshProjects();
    expect(projects.value.map((p) => p.id)).toEqual(['kc']);
  });

  it('selectProject loads the brief and stamps last_seen_at (PUT)', async () => {
    const calls: string[] = [];
    mockFetch((url, init) => {
      calls.push(`${init?.method ?? 'GET'} ${url}`);
      if (url.includes('/brief')) return briefFor('kc');
      return { id: 'kc' };
    });
    projects.value = [project({ id: 'kc' })];
    await selectProject('kc');
    expect(selectedProjectId.value).toBe('kc');
    expect(brief.value?.project.id).toBe('kc');
    // A PUT to stamp last_seen_at fired.
    expect(calls.some((c) => c.startsWith('PUT') && c.includes('/api/projects/kc'))).toBe(true);
  });

  it('selectProject(null) is the Workspace scope — no brief fetch', async () => {
    const calls: string[] = [];
    mockFetch((url) => {
      calls.push(url);
      return {};
    });
    await selectProject(null);
    expect(selectedProjectId.value).toBeNull();
    expect(brief.value).toBeNull();
    expect(calls.some((c) => c.includes('/brief'))).toBe(false);
  });

  it('reloads the brief on memory.changed so a CTO decision appears live (#469 review M1)', async () => {
    vi.useFakeTimers();
    try {
      const urls: string[] = [];
      mockFetch((url) => {
        urls.push(url);
        return url.includes('/brief') ? briefFor('kc') : {};
      });
      selectedProjectId.value = 'kc';
      // A memory.changed event (the CTO recorded a decision) must refresh the
      // brief; a projects.changed (e.g. a last_seen stamp) must NOT.
      onEvent({ type: 'memory.changed', data: {} });
      await vi.advanceTimersByTimeAsync(300);
      expect(urls.some((u) => u.includes('/api/projects/kc/brief'))).toBe(true);

      urls.length = 0;
      onEvent({ type: 'projects.changed', data: {} });
      await vi.advanceTimersByTimeAsync(300);
      expect(urls.some((u) => u.includes('/brief'))).toBe(false); // list only
    } finally {
      vi.useRealTimers();
    }
  });

  it('a late brief load does not clobber a newer selection', async () => {
    // Select 'a' but resolve its brief AFTER switching to 'b'.
    let resolveA: (v: unknown) => void = () => {};
    globalThis.fetch = vi.fn((url: string) => {
      if (String(url).includes('/api/projects/a/brief')) {
        return new Promise((res) =>
          (resolveA = res as (v: unknown) => void),
        ).then(() => ({
          ok: true, status: 200,
          headers: { get: () => 'application/json' },
          json: async () => briefFor('a'),
        }));
      }
      return Promise.resolve({
        ok: true, status: 200,
        headers: { get: () => 'application/json' },
        json: async () => (String(url).includes('/brief') ? briefFor('b') : {}),
      });
    }) as unknown as typeof fetch;

    projects.value = [project({ id: 'a' }), project({ id: 'b' })];
    const pa = selectProject('a');
    await selectProject('b'); // switch before a resolves
    resolveA({}); // now let a's brief land
    await pa;
    expect(selectedProjectId.value).toBe('b');
    expect(brief.value?.project.id).toBe('b'); // a's late brief was dropped
  });
});

/**
 * The namespace a new memory defaults to (#358). Load-bearing, not cosmetic:
 * retrieval is namespace-scoped (#359), so an entry written under `user.` while
 * working in a project is one that project's own chats will never be shown.
 */
describe('active project → default memory namespace', () => {
  afterEach(() => localStorage.clear());

  it('falls back to user. with no project selected', () => {
    projects.value = [project({ id: 'kc', memory_namespace: 'project.kc' })];
    expect(activeProject()).toBeNull();
    expect(defaultMemoryNamespace()).toBe(USER_NAMESPACE);
  });

  it("uses the selected project's memory_namespace", async () => {
    mockFetch(() => ({}));
    projects.value = [project({ id: 'kc', memory_namespace: 'project.kc' })];
    await selectProject('kc');
    expect(activeProject()?.id).toBe('kc');
    expect(defaultMemoryNamespace()).toBe('project.kc');
  });

  it('honours a custom namespace rather than assuming project.<id>', async () => {
    mockFetch(() => ({}));
    projects.value = [project({ id: 'kc', memory_namespace: 'work.kubecoder' })];
    await selectProject('kc');
    expect(defaultMemoryNamespace()).toBe('work.kubecoder');
  });

  it('remembers the project across tabs that never ran the CTO bootstrap', () => {
    // The Memory tab has its own mount; selectedProjectId is still null there,
    // so the last-selected id (persisted by the rail) is what makes the default
    // follow the user between tabs.
    localStorage.setItem('kc.cto.lastProject', 'kc');
    projects.value = [project({ id: 'kc', memory_namespace: 'project.kc' })];
    expect(selectedProjectId.value).toBeNull();
    expect(defaultMemoryNamespace()).toBe('project.kc');
  });

  it('ignores a remembered project that is no longer in the registry', () => {
    localStorage.setItem('kc.cto.lastProject', 'deleted');
    projects.value = [project({ id: 'kc' })];
    expect(activeProject()).toBeNull();
    expect(defaultMemoryNamespace()).toBe(USER_NAMESPACE);
  });

  it('falls back to user. when the registry has not loaded yet', () => {
    localStorage.setItem('kc.cto.lastProject', 'kc');
    expect(defaultMemoryNamespace()).toBe(USER_NAMESPACE);
  });

  it('returns to user. once the Workspace scope is selected', async () => {
    mockFetch(() => ({}));
    projects.value = [project({ id: 'kc', memory_namespace: 'project.kc' })];
    await selectProject('kc');
    await selectProject(null);
    expect(defaultMemoryNamespace()).toBe(USER_NAMESPACE);
  });
});
