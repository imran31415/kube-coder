import { render, screen } from '@testing-library/preact';
import { describe, expect, it, afterEach } from 'vitest';
import { BriefPanel } from './BriefPanel';
import { brief, selectedProjectId, _resetProjectsForTest } from '../../store/projects';
import type { ProjectBrief } from '../../api/projects';

const now = Math.floor(Date.now() / 1000);

const sample: ProjectBrief = {
  project: {
    id: 'kc', name: 'kube-coder', workdirs: ['/home/dev/kube-coder'],
    repo: 'imran31415/kube-coder', memory_namespace: 'project.kc',
    status: 'active', north_star: 'Ship the AI CTO', last_seen_at: null,
    created_at: now, updated_at: now,
  },
  tasks: {
    running: 2, waiting: 1, total: 5,
    recent: [
      { task_id: 't1', status: 'running', prompt: 'build the rail', workdir: '/home/dev/kube-coder', assistant: 'claude', last_activity_at: now },
    ],
  },
  goals: [
    { namespace: 'project.kc.goals', key: 'g1', value: 'Reach GA', tags: ['goal'], importance: 0.9, updated_at: now },
  ],
  decisions: [
    { namespace: 'project.kc.decisions', key: 'sse', value: 'SSE over websockets', tags: ['decision'], importance: 0.8, updated_at: now },
  ],
  memories: [],
  git: [{ workdir: '/home/dev/kube-coder', branch: 'feat/466-cto-page', exists: true }],
  triggers: [],
  counts: { goals: 1, decisions: 1, memories: 0, tasks: 5 },
  brief_markdown: '# kube-coder',
};

afterEach(() => _resetProjectsForTest());

describe('BriefPanel', () => {
  it('renders the workspace-scope empty state when no project is selected', () => {
    selectedProjectId.value = null;
    brief.value = null;
    render(<BriefPanel />);
    expect(screen.getByText('Workspace scope')).toBeTruthy();
  });

  it('renders north star, task stats, goals, decisions, tasks and git from the brief', () => {
    selectedProjectId.value = 'kc';
    brief.value = sample;
    render(<BriefPanel />);
    expect(screen.getByText('Ship the AI CTO')).toBeTruthy();
    expect(screen.getByText('Reach GA')).toBeTruthy();
    expect(screen.getByText('SSE over websockets')).toBeTruthy();
    expect(screen.getByText('build the rail')).toBeTruthy();
    expect(screen.getByText('feat/466-cto-page')).toBeTruthy();
    // Task stat counts render.
    expect(screen.getByText('2')).toBeTruthy();
  });

  it('deep-links a recent task to /tasks/<id>', () => {
    selectedProjectId.value = 'kc';
    brief.value = sample;
    render(<BriefPanel />);
    const link = screen.getByTitle('build the rail') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('/tasks/t1');
  });

  it('renders a date on each decision-log entry (#467 timeline)', () => {
    selectedProjectId.value = 'kc';
    brief.value = sample;
    const { container } = render(<BriefPanel />);
    const dates = container.querySelectorAll('.cto-brief-decisions .cto-brief-date');
    expect(dates.length).toBe(sample.decisions.length);
    expect((dates[0].textContent ?? '').length).toBeGreaterThan(0);
  });
});
