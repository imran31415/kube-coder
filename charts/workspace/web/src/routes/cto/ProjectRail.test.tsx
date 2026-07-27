import { render, screen, fireEvent } from '@testing-library/preact';
import { describe, expect, it, afterEach, vi } from 'vitest';
import { ProjectRail, projectInitials } from './ProjectRail';
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

  it('exposes a labelled collapse control only when the surface can collapse it', () => {
    projects.value = [];
    const { rerender } = render(<ProjectRail selectedId={null} onSelect={() => {}} />);
    expect(screen.queryByRole('button', { name: 'Collapse projects rail' })).toBeNull();

    const onToggle = vi.fn();
    rerender(<ProjectRail selectedId={null} onSelect={() => {}} onToggleCollapse={onToggle} />);
    const btn = screen.getByRole('button', { name: 'Collapse projects rail' });
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(btn.getAttribute('aria-controls')).toBe('cto-project-rail');
    fireEvent.click(btn);
    expect(onToggle).toHaveBeenCalled();
  });
});

describe('ProjectRail collapsed (#530)', () => {
  it('renders an icon strip of initials that still switches project', () => {
    const onSelect = vi.fn();
    projects.value = [
      project({ id: 'kc', name: 'kube-coder' }),
      project({ id: 'h', name: 'hosted' }),
    ];
    const { container } = render(
      <ProjectRail selectedId="kc" onSelect={onSelect} collapsed onToggleCollapse={() => {}} />,
    );
    expect(container.querySelector('.cto-rail-collapsed')).toBeTruthy();
    expect(screen.getByText('KC')).toBeTruthy();
    expect(screen.getByText('HO')).toBeTruthy();
    // Names survive as tooltips + accessible labels, not visible text.
    const card = screen.getByRole('button', { name: 'kube-coder' });
    expect(card.getAttribute('title')).toBe('kube-coder');
    expect(card.getAttribute('aria-current')).toBe('true');

    fireEvent.click(screen.getByRole('button', { name: 'hosted' }));
    expect(onSelect).toHaveBeenCalledWith('h');
    fireEvent.click(screen.getByRole('button', { name: 'Workspace' }));
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it('keeps the pulse visible as a dot on the collapsed avatar', () => {
    projects.value = [
      project({ id: 'kc', name: 'kube-coder', pulse: { running: 1, waiting: 0, last_activity_at: now } }),
      project({ id: 'q', name: 'quiet' }),
    ];
    const { container } = render(<ProjectRail selectedId={null} onSelect={() => {}} collapsed />);
    expect(container.querySelectorAll('.cto-icon-pulse').length).toBe(1);
    expect(container.querySelector('.cto-icon-pulse .cto-dot-running')).toBeTruthy();
  });

  it('the expand control reports its collapsed state', () => {
    projects.value = [];
    render(<ProjectRail selectedId={null} onSelect={() => {}} collapsed onToggleCollapse={() => {}} />);
    const btn = screen.getByRole('button', { name: 'Expand projects rail' });
    expect(btn.getAttribute('aria-expanded')).toBe('false');
  });
});

describe('projectInitials', () => {
  it('takes the first two words, or the first two letters of one', () => {
    expect(projectInitials('kube-coder')).toBe('KC');
    expect(projectInitials('hosted')).toBe('HO');
    expect(projectInitials('my new app')).toBe('MN');
    expect(projectInitials('a')).toBe('A');
    expect(projectInitials('  ')).toBe('?');
    expect(projectInitials('')).toBe('?');
  });
});
