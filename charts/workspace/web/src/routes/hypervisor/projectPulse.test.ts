import { describe, expect, it } from 'vitest';
import type { Project } from '../../api/projects';
import { hasUnseenActivity, pulseLabel, pulseOf } from './projectPulse';

function project(over: Partial<Project> = {}): Project {
  return {
    id: 'kc',
    name: 'kube-coder',
    workdirs: [],
    repo: '',
    memory_namespace: 'project.kc',
    status: 'active',
    north_star: '',
    last_seen_at: null,
    created_at: 0,
    updated_at: 0,
    ...over,
  };
}

describe('pulseOf', () => {
  it('reads the counts the list endpoint embeds', () => {
    expect(
      pulseOf(project({ pulse: { running: 2, waiting: 1, last_activity_at: 10 } })),
    ).toEqual({ running: 2, waiting: 1 });
  });

  it('is quiet for a record with no pulse at all', () => {
    expect(pulseOf(project())).toEqual({ running: 0, waiting: 0 });
    expect(pulseOf(undefined)).toEqual({ running: 0, waiting: 0 });
  });
});

describe('hasUnseenActivity', () => {
  it('is true when the project moved after your last visit', () => {
    expect(
      hasUnseenActivity(
        project({ last_seen_at: 100, pulse: { running: 0, waiting: 0, last_activity_at: 200 } }),
      ),
    ).toBe(true);
  });

  it('is false when nothing happened since', () => {
    expect(
      hasUnseenActivity(
        project({ last_seen_at: 300, pulse: { running: 0, waiting: 0, last_activity_at: 200 } }),
      ),
    ).toBe(false);
  });

  it('is false for a project you have never opened', () => {
    // Otherwise every project in a fresh workspace lights up at once, which
    // tells the user nothing.
    expect(
      hasUnseenActivity(
        project({ last_seen_at: null, pulse: { running: 0, waiting: 0, last_activity_at: 200 } }),
      ),
    ).toBe(false);
  });

  it('is false when the project has no recorded activity', () => {
    expect(
      hasUnseenActivity(
        project({ last_seen_at: 100, pulse: { running: 0, waiting: 0, last_activity_at: null } }),
      ),
    ).toBe(false);
  });
});

describe('pulseLabel', () => {
  it('says nothing when the project is quiet', () => {
    expect(pulseLabel({ running: 0, waiting: 0 }, false)).toBe('');
  });

  it('names each signal that is actually present', () => {
    expect(pulseLabel({ running: 2, waiting: 0 }, false)).toBe('2 running');
    expect(pulseLabel({ running: 0, waiting: 1 }, false)).toBe('1 waiting on you');
    expect(pulseLabel({ running: 2, waiting: 1 }, true)).toBe(
      '2 running · 1 waiting on you · new activity since you were last here',
    );
  });
});
