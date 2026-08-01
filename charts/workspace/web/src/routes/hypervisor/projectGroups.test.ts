import { describe, expect, it } from 'vitest';
import { groupByProject, isUngrouped } from './projectGroups';
import type { HypervisorThread, ThreadStatus } from '../../api/hypervisor';
import type { Project } from '../../api/projects';

const NOW = 1_700_000_000; // fixed "now" in seconds

function thread(over: Partial<HypervisorThread> = {}): HypervisorThread {
  return {
    id: 'a',
    title: 'chat',
    assistant: 'claude',
    status: 'idle' as ThreadStatus,
    created_at: NOW,
    updated_at: NOW,
    ...over,
  };
}

function project(over: Partial<Project> = {}): Project {
  return {
    id: 'p',
    name: 'P',
    workdirs: [],
    repo: '',
    memory_namespace: 'project.p',
    status: 'active',
    north_star: '',
    last_seen_at: null,
    created_at: NOW,
    updated_at: NOW,
    ...over,
  };
}

const PROJECTS = [
  project({ id: 'kc', name: 'kube-coder' }),
  project({ id: 'pool', name: 'Pool Hall' }),
];

describe('groupByProject', () => {
  it('groups by project_id and names each group from the registry', () => {
    const groups = groupByProject(
      [
        thread({ id: '1', project_id: 'kc' }),
        thread({ id: '2', project_id: 'pool' }),
        thread({ id: '3', project_id: 'kc' }),
      ],
      PROJECTS,
    );
    expect(groups.map((g) => g.label)).toEqual(['kube-coder', 'Pool Hall']);
    expect(groups[0].threads.map((t) => t.id)).toEqual(['1', '3']);
  });

  it('collects unfiled chats into a trailing "No project" group', () => {
    const groups = groupByProject(
      [thread({ id: '1' }), thread({ id: '2', project_id: 'kc' })],
      PROJECTS,
    );
    expect(groups.map((g) => g.id)).toEqual(['kc', '']);
    expect(groups[1].label).toBe('No project');
  });

  it('orders project groups by their most recently touched chat', () => {
    const groups = groupByProject(
      [
        thread({ id: 'old', project_id: 'kc', updated_at: NOW - 9999 }),
        thread({ id: 'new', project_id: 'pool', updated_at: NOW }),
      ],
      PROJECTS,
    );
    expect(groups.map((g) => g.id)).toEqual(['pool', 'kc']);
  });

  it('falls back to created_at, then 0, when updated_at is missing', () => {
    const groups = groupByProject(
      [
        thread({ id: 'none', project_id: 'kc', updated_at: null, created_at: null }),
        thread({ id: 'created', project_id: 'pool', updated_at: null, created_at: NOW }),
      ],
      PROJECTS,
    );
    expect(groups.map((g) => g.id)).toEqual(['pool', 'kc']);
  });

  it('labels a binding the registry no longer knows with its raw id', () => {
    // A project archived or deleted out from under its chats must still show
    // where those chats are filed rather than silently merging into "No project".
    const groups = groupByProject([thread({ project_id: 'ghost' })], PROJECTS);
    expect(groups[0].id).toBe('ghost');
    expect(groups[0].label).toBe('ghost');
  });

  it('preserves the incoming (newest-first) order inside a group', () => {
    const groups = groupByProject(
      [
        thread({ id: 'newest', project_id: 'kc', updated_at: NOW }),
        thread({ id: 'older', project_id: 'kc', updated_at: NOW - 500 }),
      ],
      PROJECTS,
    );
    expect(groups[0].threads.map((t) => t.id)).toEqual(['newest', 'older']);
  });
});

describe('isUngrouped', () => {
  it('is true when nothing is filed — the sidebar stays a flat list', () => {
    const groups = groupByProject([thread({ id: '1' }), thread({ id: '2' })], []);
    expect(groups).toHaveLength(1);
    expect(isUngrouped(groups)).toBe(true);
  });

  it('is true for an empty list (no headers over an empty sidebar)', () => {
    expect(isUngrouped(groupByProject([], PROJECTS))).toBe(true);
  });

  it('is false as soon as one chat is filed', () => {
    const groups = groupByProject(
      [thread({ id: '1' }), thread({ id: '2', project_id: 'kc' })],
      PROJECTS,
    );
    expect(isUngrouped(groups)).toBe(false);
  });
});
