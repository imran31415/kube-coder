import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { ConnectBoard } from './ConnectBoard';
import { BoardRail } from './BoardRail';
import { _resetBoardsForTest, boards } from '../../store/boards';
import { serverMode } from '../../store/server-mode';
import type { BoardTemplate } from '../../api/boards';

/**
 * Connecting a board without writing any JSON (#588 Phase 7).
 *
 * The templates and the create/test-fetch routes all existed; what did not was
 * any way to reach them from the product. The empty rail told a new user to
 * "ask an agent to build one", which is the right answer for a tracker nobody
 * has a connector for and the wrong one for the three that ship.
 *
 * The invariant these tests exist to hold is the last step: the flow finishes
 * with a real `test-fetch`, and the dialog reports what came back. A board
 * that has been SAVED has demonstrated nothing — the whole feature rests on
 * that distinction and it is the easiest one to quietly drop.
 */

const realFetch = globalThis.fetch;

const TEMPLATES: BoardTemplate[] = [
  {
    id: 'jira-cloud',
    display_name: 'Jira Cloud',
    vendor: 'jira',
    actions: ['comment', 'set_status'],
    needs: ['Replace YOURSITE with your Atlassian site.'],
    placeholders: [
      { token: 'YOURSITE', label: 'Atlassian site', help: 'The label only.', example: 'acme' },
      { token: 'PROJ', label: 'Project key', help: 'SUP-142 → SUP.', example: 'SUP' },
    ],
    credential: {
      name: 'JIRA_API_TOKEN',
      format: 'basic',
      username_label: 'Atlassian account email',
      secret_label: 'API token',
      help: 'id.atlassian.com → Security → API tokens.',
    },
    verified: false,
    note: 'a starting point, not a verified connector',
  },
  {
    id: 'github-issues',
    display_name: 'GitHub Issues',
    vendor: 'github',
    actions: ['comment'],
    needs: ['Replace OWNER and REPO.'],
    placeholders: [
      { token: 'OWNER', label: 'Owner', help: 'The org.', example: 'acme' },
      { token: 'REPO', label: 'Repository', help: 'The repo.', example: 'billing-api' },
    ],
    credential: null,
    verified: false,
    note: 'a starting point, not a verified connector',
  },
];

/** Records every call so a test can assert the ORDER, which is the part that
 *  matters: the credential has to be stored before test-fetch runs, or the
 *  verification fails for a reason that has nothing to do with the
 *  connector. */
let calls: { method: string; url: string; body: unknown }[] = [];

function mockApi(over: Record<string, () => unknown> = {}) {
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(init.body as string) : null;
    calls.push({ method, url, body });
    const path = url.split('?')[0];
    const custom = Object.entries(over).find(([k]) => {
      const [m, p] = k.split(' ');
      return m === method && path.endsWith(p);
    })?.[1];
    const payload = custom
      ? custom()
      : url.endsWith('/api/boards/templates')
        ? { templates: TEMPLATES }
        : url.endsWith('/fill')
          ? { connector: { id: 'jira-sup', vendor: 'jira' }, verified: false }
          : url.endsWith('/test-fetch')
            ? { items: [{ id: '1' }, { id: '2' }], complete: true, pages_fetched: 1 }
            : url.endsWith('/api/boards') && method === 'POST'
              ? { id: 'jira-sup', vendor: 'jira', display_name: 'Jira Cloud — acme/SUP' }
              : url.includes('/api/boards/credentials')
                ? { credentials: [] }
                : { boards: [] };
    if (payload instanceof Error) throw payload;
    return {
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    };
  }) as unknown as typeof fetch;
}

async function fillJira() {
  render(<ConnectBoard open onClose={() => {}} />);
  fireEvent.click(await screen.findByText('Jira Cloud'));
  fireEvent.input(screen.getByPlaceholderText('acme'), { target: { value: 'acme' } });
  fireEvent.input(screen.getByPlaceholderText('SUP'), { target: { value: 'SUP' } });
  fireEvent.input(screen.getByPlaceholderText('you@example.com'), {
    target: { value: 'ops@acme.com' },
  });
  fireEvent.input(screen.getByPlaceholderText('paste the raw token'), {
    target: { value: 'the-token' },
  });
}

describe('connecting a board', () => {
  beforeEach(() => {
    _resetBoardsForTest();
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic' };
    calls = [];
    mockApi();
  });

  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('offers the shipped templates', async () => {
    render(<ConnectBoard open onClose={() => {}} />);
    expect(await screen.findByText('Jira Cloud')).toBeInTheDocument();
    expect(screen.getByText('GitHub Issues')).toBeInTheDocument();
  });

  it('asks only for the blanks the chosen template declares', async () => {
    render(<ConnectBoard open onClose={() => {}} />);
    fireEvent.click(await screen.findByText('GitHub Issues'));
    expect(screen.getByText('Owner')).toBeInTheDocument();
    expect(screen.getByText('Repository')).toBeInTheDocument();
    // ...and not Jira's, which is the point of generating the form from the
    // template rather than switch-casing per vendor.
    expect(screen.queryByText('Project key')).not.toBeInTheDocument();
  });

  it('does not ask for a credential the workspace already brokers', async () => {
    render(<ConnectBoard open onClose={() => {}} />);
    fireEvent.click(await screen.findByText('GitHub Issues'));
    expect(screen.queryByPlaceholderText('paste the raw token')).toBeNull();
    expect(screen.getByText(/Nothing to paste/i)).toBeInTheDocument();
  });

  it('asks for the username half when the connector composes Basic', async () => {
    render(<ConnectBoard open onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Jira Cloud'));
    expect(screen.getByText('Atlassian account email')).toBeInTheDocument();
  });

  it('will not submit until every blank is answered', async () => {
    render(<ConnectBoard open onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Jira Cloud'));
    const submit = screen.getByRole('button', { name: /connect and fetch/i });
    expect(submit).toBeDisabled();
    fireEvent.input(screen.getByPlaceholderText('acme'), { target: { value: 'acme' } });
    expect(submit).toBeDisabled();
  });

  it('stores the credential BEFORE it verifies, then verifies', async () => {
    await fillJira();
    fireEvent.click(screen.getByRole('button', { name: /connect and fetch/i }));
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes('/test-fetch'))).toBe(true),
    );
    const order = calls
      .filter((c) => c.method !== 'GET')
      .map((c) => c.url.replace(/^.*\/api\/boards/, ''));
    expect(order).toEqual([
      '/credentials/JIRA_API_TOKEN',
      '/templates/jira-cloud/fill',
      '',
      '/jira-sup/test-fetch',
    ]);
  });

  it('sends the answers as values for the SERVER to substitute', async () => {
    await fillJira();
    fireEvent.click(screen.getByRole('button', { name: /connect and fetch/i }));
    await waitFor(() =>
      expect(calls.some((c) => c.url.endsWith('/fill'))).toBe(true),
    );
    const fill = calls.find((c) => c.url.endsWith('/fill'));
    // Deliberately NOT a connector assembled in the browser: these values land
    // inside URL paths and Jira's JQL string, and only the server checks what
    // they may contain.
    expect(fill?.body).toEqual({
      values: { YOURSITE: 'acme', PROJ: 'SUP' },
      id: '',
      display_name: '',
    });
  });

  it('reports what the fetch actually returned, not that it saved', async () => {
    await fillJira();
    fireEvent.click(screen.getByRole('button', { name: /connect and fetch/i }));
    expect(await screen.findByText(/it fetched/i)).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('keeps a created board whose first fetch failed, and says so', async () => {
    mockApi({
      'POST /api/boards/jira-sup/test-fetch': () => {
        throw new Error('401 Unauthorized');
      },
    });
    await fillJira();
    fireEvent.click(screen.getByRole('button', { name: /connect and fetch/i }));
    // The board is NOT rolled back: the connector is usually right and the
    // credential wrong, and discarding it makes the operator retype every
    // answer to find out which.
    expect(await screen.findByText(/created but its first fetch failed/i))
      .toBeInTheDocument();
    expect(screen.getByRole('button', { name: /open jira-sup/i })).toBeInTheDocument();
  });

  it('does not claim a fetch happened when the create failed', async () => {
    mockApi({
      'POST /api/boards': () => {
        throw new Error('board \'jira-sup\' already exists');
      },
    });
    await fillJira();
    fireEvent.click(screen.getByRole('button', { name: /connect and fetch/i }));
    expect(await screen.findByText(/already exists/i)).toBeInTheDocument();
    expect(screen.queryByText(/it fetched/i)).toBeNull();
    expect(calls.some((c) => c.url.includes('/test-fetch'))).toBe(false);
  });

  it('is reachable from the rail, which is the whole point', () => {
    boards.value = [];
    render(<BoardRail onConnect={() => {}} />);
    expect(
      screen.getByRole('button', { name: /connect a board/i }),
    ).toBeInTheDocument();
  });
});
