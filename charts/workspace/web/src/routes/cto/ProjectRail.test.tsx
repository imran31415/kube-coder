import { render, screen, fireEvent } from '@testing-library/preact';
import { describe, expect, it, afterEach, vi } from 'vitest';
import { ProjectRail } from './ProjectRail';
import { projects, projectsLoading, _resetProjectsForTest } from '../../store/projects';
import type { Project } from '../../api/projects';

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

afterEach(() => _resetProjectsForTest());

describe('ProjectRail', () => {
  it('always shows the Workspace root scope', () => {
    projects.value = [];
    render(<ProjectRail selectedId={null} onSelect={() => {}} />);
    expect(screen.getByText('Workspace')).toBeTruthy();
  });

  it('renders project cards with pulse counts and hides archived ones', () => {
    projects.value = [
      project({ id: 'kc', name: 'kube-coder', repo: 'o/kc', pulse: { running: 2, waiting: 1, last_activity_at: now } }),
      project({ id: 'old', name: 'old', status: 'archived' }),
    ];
    render(<ProjectRail selectedId="kc" onSelect={() => {}} />);
    expect(screen.getByText('kube-coder')).toBeTruthy();
    expect(screen.queryByText('old')).toBeNull(); // archived filtered out
    expect(screen.getByText('2 running')).toBeTruthy();
    expect(screen.getByText('1 waiting')).toBeTruthy();
  });

  it('shows skeleton cards while loading with an empty registry (no spinner)', () => {
    projects.value = [];
    projectsLoading.value = true;
    const { container } = render(<ProjectRail selectedId={null} onSelect={() => {}} />);
    expect(container.querySelectorAll('.cto-card-skeleton').length).toBeGreaterThan(0);
    projectsLoading.value = false;
  });

  it('calls onSelect with the project id when a card is clicked', () => {
    const onSelect = vi.fn();
    projects.value = [project({ id: 'kc', name: 'kube-coder' })];
    render(<ProjectRail selectedId={null} onSelect={onSelect} />);
    fireEvent.click(screen.getByText('kube-coder'));
    expect(onSelect).toHaveBeenCalledWith('kc');
  });

  it('calls onSelect(null) for the Workspace scope', () => {
    const onSelect = vi.fn();
    projects.value = [project({ id: 'kc' })];
    render(<ProjectRail selectedId="kc" onSelect={onSelect} />);
    fireEvent.click(screen.getByText('Workspace'));
    expect(onSelect).toHaveBeenCalledWith(null);
  });
});
