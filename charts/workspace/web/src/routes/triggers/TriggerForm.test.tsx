import { render, screen, waitFor, within } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { TriggersRoute, describeBoardRun, parseMatchPayload, samplePayload } from './index';
import { triggers, triggerFilter, stopTriggerPolling } from '../../store/triggers';
import { drawerOpen } from '../../store/ui';
import { serverMode } from '../../store/server-mode';

/**
 * The "What should happen?" branch of the New trigger form (#709 part 2): a
 * cron or webhook can start a Board run instead of a Build. What matters:
 * the Build form is unchanged, the Board-run form sends `board_run` and no
 * prompt, propose mode is fixed text rather than a choice, and a filtered
 * webhook's Test sends the event the filter needs.
 */

const api = vi.hoisted(() => ({
  createCron: vi.fn(),
  createWebhook: vi.fn(),
  testWebhook: vi.fn(),
  listBoards: vi.fn(),
}));
vi.mock('../../api/triggers', async (orig) => ({
  ...(await orig<typeof import('../../api/triggers')>()),
  createCron: (...a: unknown[]) => api.createCron(...a),
  createWebhook: (...a: unknown[]) => api.createWebhook(...a),
  testWebhook: (...a: unknown[]) => api.testWebhook(...a),
}));
vi.mock('../../api/boards', async (orig) => ({
  ...(await orig<typeof import('../../api/boards')>()),
  listBoards: (...a: unknown[]) => api.listBoards(...a),
}));

const realFetch = globalThis.fetch;
let webhooks: unknown[] = [];

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  api.createCron.mockResolvedValue({ id: 'x' });
  api.createWebhook.mockResolvedValue({ id: 'x' });
  api.testWebhook.mockResolvedValue({ run_id: 'run-1-abcd', board_id: 'acme' });
  api.listBoards.mockResolvedValue({
    boards: [{ id: 'acme', display_name: 'Acme issues' }, { id: 'ops', display_name: 'Ops' }],
  });
  webhooks = [];
  // Every mutation control sits inside MutatorOnly; model a writable deploy.
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic' };
  triggers.value = [];
  triggerFilter.value = '';
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes('/api/webhooks')
      ? { webhooks }
      : url.includes('/api/crons') ? { crons: [] } : { page_watches: [] };
    return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => body };
  }) as unknown as typeof fetch;
});

afterEach(() => {
  stopTriggerPolling();
  globalThis.fetch = realFetch;
  drawerOpen.value = null;
});

async function openForm() {
  render(<TriggersRoute />);
  await userEvent.click(screen.getByRole('button', { name: /New trigger/ }));
  return screen.getByRole('group', { name: 'What should happen?' });
}

async function fill(label: RegExp, value: string) {
  const el = screen.getByLabelText(label);
  await userEvent.clear(el);
  await userEvent.type(el, value);
}

describe('New trigger — What should happen?', () => {
  it('defaults to a Build and sends exactly what it always did', async () => {
    const group = await openForm();
    expect(within(group).getByRole('button', { name: 'Start a Build' })).toHaveAttribute('aria-pressed', 'true');
    await fill(/^ID/, 'hourly');
    await userEvent.type(screen.getByPlaceholderText(/What should Claude do/), 'Summarise');
    await userEvent.click(screen.getByRole('button', { name: /Create cron/ }));
    await waitFor(() => expect(api.createCron).toHaveBeenCalledTimes(1));
    expect(api.createCron).toHaveBeenCalledWith({
      id: 'hourly', schedule: '0 * * * *', prompt_template: 'Summarise',
      workdir: '/home/dev', timezone: 'UTC',
    });
    expect(api.listBoards).not.toHaveBeenCalled();
  });

  it('a Board-run cron picks a board, sends board_run and no prompt', async () => {
    const group = await openForm();
    await userEvent.click(within(group).getByRole('button', { name: 'Start a Board run' }));
    expect(screen.queryByPlaceholderText(/What should Claude do/)).not.toBeInTheDocument();
    const picker = await screen.findByRole('combobox');
    await userEvent.selectOptions(picker, 'ops');
    await fill(/^ID/, 'nightly-ai');
    await fill(/^Tags/, 'ai, triage');
    await fill(/^Limit/, '10');
    await fill(/^At once/, '2');
    await userEvent.click(screen.getByRole('button', { name: /Create cron/ }));
    await waitFor(() => expect(api.createCron).toHaveBeenCalledTimes(1));
    expect(api.createCron).toHaveBeenCalledWith({
      id: 'nightly-ai', schedule: '0 * * * *', timezone: 'UTC', action: 'board_run',
      board_run: { board_id: 'ops', select: { tags: ['ai', 'triage'], limit: 10 }, concurrency: 2 },
    });
  });

  it('shows propose mode as fixed text, never a toggle', async () => {
    const group = await openForm();
    await userEvent.click(within(group).getByRole('button', { name: 'Start a Board run' }));
    expect(screen.getByText(/Every write waits for your approval/)).toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.queryByText(/autonomous/i)).not.toBeInTheDocument();
  });

  it('refuses an out-of-range concurrency before submit', async () => {
    const group = await openForm();
    await userEvent.click(within(group).getByRole('button', { name: 'Start a Board run' }));
    await screen.findByRole('combobox');
    await fill(/^ID/, 'nightly-ai');
    await fill(/^At once/, '20');
    expect(screen.getByText('1 to 8')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Create cron/ })).toBeDisabled();
  });

  it('a Board-run webhook sends its filter and item id, then shows the secret once', async () => {
    api.createWebhook.mockResolvedValue({
      id: 'gh-ai', receive_url: 'https://ws.example/api/webhooks/gh-ai', hmac_secret_once: 's3cr3t-full-value',
    });
    await openForm();
    await userEvent.click(screen.getByRole('button', { name: 'webhook' }));
    await userEvent.click(screen.getByRole('button', { name: 'Start a Board run' }));
    await screen.findByRole('combobox');
    await fill(/^ID/, 'gh-ai');
    await fill(/^Only for event/, 'issues');
    await fill(/^Item id from/, 'issue.id');
    await userEvent.type(screen.getByLabelText(/^Only when the payload has/), 'action = labeled{enter}label.name = ai');
    expect(screen.getByText(/Pair this webhook with a nightly cron/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Create webhook/ }));
    await waitFor(() => expect(api.createWebhook).toHaveBeenCalledTimes(1));
    expect(api.createWebhook).toHaveBeenCalledWith({
      id: 'gh-ai', action: 'board_run',
      board_run: { board_id: 'acme', select: { limit: 5 }, concurrency: 1 },
      item_id_from: 'issue.id',
      match: { event: 'issues', payload: { action: 'labeled', 'label.name': 'ai' } },
    });
    // The full secret, not a truncated toast: GitHub needs all of it.
    expect(await screen.findByDisplayValue('s3cr3t-full-value')).toBeInTheDocument();
    expect(screen.getByDisplayValue('https://ws.example/api/webhooks/gh-ai')).toBeInTheDocument();
  });

  it('a malformed filter line is an error, not a silent no-match', async () => {
    await openForm();
    await userEvent.click(screen.getByRole('button', { name: 'webhook' }));
    await fill(/^ID/, 'gh');
    await userEvent.type(screen.getByPlaceholderText(/What should Claude do/), 'Review');
    await userEvent.type(screen.getByLabelText(/^Only when the payload has/), 'labeled');
    expect(screen.getByText('"labeled" is not path = value')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Create webhook/ })).toBeDisabled();
  });

  it('page watches only start Builds', async () => {
    await openForm();
    await userEvent.click(screen.getByRole('button', { name: 'page-watch' }));
    expect(screen.queryByRole('group', { name: 'What should happen?' })).not.toBeInTheDocument();
  });
});

describe('Triggers list — Board-run rows', () => {
  const ghAi = {
    id: 'gh-ai', prompt_template: '', action: 'board_run',
    board_run: { board_id: 'acme', select: { tags: ['ai'], limit: 5 }, concurrency: 1 },
    match: { event: 'issues', payload: { action: 'labeled', 'label.name': 'ai' } },
    item_id_from: 'issue.id',
  };

  it('describes the run instead of an empty prompt', async () => {
    webhooks = [ghAi];
    render(<TriggersRoute />);
    expect(await screen.findByText(/Board run on acme · tags ai · up to 5/)).toBeInTheDocument();
    expect(screen.getByText(/event issues · action = labeled · label.name = ai · item from issue.id/))
      .toBeInTheDocument();
  });

  it('Test opens a form whose event and payload satisfy the filter', async () => {
    webhooks = [ghAi];
    render(<TriggersRoute />);
    await userEvent.click(await screen.findByRole('button', { name: /^Test$/ }));
    const form = screen.getByRole('form', { name: 'Test gh-ai' });
    expect(within(form).getByLabelText(/^Event/)).toHaveValue('issues');
    await userEvent.click(within(form).getByRole('button', { name: /Send test/ }));
    await waitFor(() => expect(api.testWebhook).toHaveBeenCalledTimes(1));
    const [id, body] = api.testWebhook.mock.calls[0];
    expect(id).toBe('gh-ai');
    expect(body.event).toBe('issues');
    expect(body.payload).toMatchObject({ action: 'labeled', label: { name: 'ai' } });
    expect(body.payload.issue).toHaveProperty('id');
  });

  it('a plain Build webhook keeps its one-click Fire now', async () => {
    webhooks = [{ id: 'plain', prompt_template: 'Review' }];
    api.testWebhook.mockResolvedValue({ task_id: 't-1' });
    render(<TriggersRoute />);
    await userEvent.click(await screen.findByRole('button', { name: /Fire now/ }));
    await waitFor(() => expect(api.testWebhook).toHaveBeenCalledWith('plain', {}));
  });
});

describe('helpers', () => {
  it('parseMatchPayload reads path = value lines', () => {
    expect(parseMatchPayload('action = labeled\n\n label.name=ai ')).toEqual({
      payload: { action: 'labeled', 'label.name': 'ai' }, error: '',
    });
    expect(parseMatchPayload('= x').error).toMatch(/not path = value/);
  });

  it('samplePayload nests dot-paths', () => {
    expect(samplePayload({ match: { payload: { 'label.name': 'ai' } }, item_id_from: 'issue.id' }))
      .toEqual({ label: { name: 'ai' }, issue: { id: 'REPLACE-with-an-item-id' } });
  });

  it('describeBoardRun always says writes wait for approval', () => {
    expect(describeBoardRun({ board_id: 'acme' })).toBe(
      'Board run on acme · every write waits for your approval');
  });
});
