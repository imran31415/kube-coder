import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/preact';

/**
 * The AI CTO's per-project assistant setup (#483, #362): a gear in the topbar
 * that picks the provider/model/effort for the session and can write the choice
 * back onto the project record.
 */

const { updateProject, listProjects, getProjectBrief, discoverProjects } =
  vi.hoisted(() => ({
    updateProject: vi.fn(),
    listProjects: vi.fn(),
    getProjectBrief: vi.fn(),
    discoverProjects: vi.fn(),
  }));

vi.mock('../../api/projects', () => ({
  updateProject: (...a: unknown[]) => updateProject(...a),
  listProjects: (...a: unknown[]) => listProjects(...a),
  getProjectBrief: (...a: unknown[]) => getProjectBrief(...a),
  discoverProjects: (...a: unknown[]) => discoverProjects(...a),
}));
vi.mock('../../api/events', () => ({
  subscribeEvents: vi.fn(() => () => {}),
  eventStreamConnected: { value: false },
}));

import { CtoConfig, matchesProjectDefaults } from './CtoConfig';
import { config, ctoAssistant, ctoModel, ctoEffort } from '../../store/hypervisor';
import { projects } from '../../store/projects';
import type { Project } from '../../api/projects';

const PROJECT: Project = {
  id: 'kube-coder',
  name: 'kube-coder',
  workdirs: ['/home/dev/kube-coder'],
  repo: '',
  memory_namespace: 'project.kube-coder',
  status: 'active',
  north_star: '',
  default_assistant: '',
  default_model: '',
  default_effort: '',
  last_seen_at: null,
  created_at: 0,
  updated_at: 0,
};

beforeEach(() => {
  vi.clearAllMocks();
  config.value = {
    enabled: true,
    defaultAssistant: 'claude',
    defaultEffort: 'high',
    workdir: '/home/dev',
    readOnly: false,
    assistants: [
      {
        id: 'claude',
        label: 'Claude Code',
        models: ['default', 'opus'],
        efforts: ['low', 'medium', 'high', 'xhigh', 'max'],
      },
      { id: 'ante', label: 'Ante CLI', models: [], efforts: [] },
    ],
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
  ctoAssistant.value = 'claude';
  ctoModel.value = 'opus';
  ctoEffort.value = 'xhigh';
  projects.value = [PROJECT];
  listProjects.mockResolvedValue([PROJECT]);
  updateProject.mockResolvedValue(PROJECT);
});

function open(project: Project | null = PROJECT) {
  const r = render(<CtoConfig project={project} />);
  fireEvent.click(r.getByLabelText('Assistant setup'));
  return r;
}

describe('CtoConfig', () => {
  it('summarises the current selection on the trigger', () => {
    const { getByLabelText } = render(<CtoConfig project={PROJECT} />);
    expect(getByLabelText('Assistant setup').textContent).toContain('Claude Code');
    expect(getByLabelText('Assistant setup').textContent).toContain('opus');
  });

  it('opens a picker carrying all three dials', () => {
    const { getByLabelText } = open();
    expect((getByLabelText('Assistant') as HTMLSelectElement).value).toBe('claude');
    expect((getByLabelText('Model') as HTMLSelectElement).value).toBe('opus');
    expect((getByLabelText('Reasoning effort') as HTMLSelectElement).value).toBe(
      'xhigh',
    );
  });

  it('persists the selection as the project default', async () => {
    const { getByText } = open();
    fireEvent.click(getByText('Set as project default'));
    await waitFor(() =>
      expect(updateProject).toHaveBeenCalledWith('kube-coder', {
        default_assistant: 'claude',
        default_model: 'opus',
        default_effort: 'xhigh',
      }),
    );
  });

  it('says so instead of re-saving when it already is the default', () => {
    const configured = {
      ...PROJECT,
      default_assistant: 'claude',
      default_model: 'opus',
      default_effort: 'xhigh',
    };
    const { getByText } = open(configured);
    const btn = getByText('Is the project default') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('explains that the Workspace scope has nothing to save onto', () => {
    const { getByText, queryByText } = open(null);
    expect(queryByText('Set as project default')).toBeNull();
    expect(getByText('Select a project to save this as its default.')).toBeTruthy();
  });

  it('surfaces a save failure rather than silently dropping it', async () => {
    updateProject.mockRejectedValue(new Error('registry is read-only'));
    const { getByText, findByRole } = open();
    fireEvent.click(getByText('Set as project default'));
    expect((await findByRole('alert')).textContent).toContain('read-only');
  });
});

describe('matchesProjectDefaults', () => {
  const sel = { assistant: 'claude', model: 'opus', effort: 'xhigh' };

  it('is true only when all three match what is stored', () => {
    expect(
      matchesProjectDefaults(
        {
          ...PROJECT,
          default_assistant: 'claude',
          default_model: 'opus',
          default_effort: 'xhigh',
        },
        sel,
      ),
    ).toBe(true);
    expect(
      matchesProjectDefaults(
        { ...PROJECT, default_assistant: 'claude', default_model: 'opus' },
        sel,
      ),
    ).toBe(false);
  });

  it('is false for the Workspace scope, which stores nothing', () => {
    expect(matchesProjectDefaults(null, sel)).toBe(false);
  });
});
