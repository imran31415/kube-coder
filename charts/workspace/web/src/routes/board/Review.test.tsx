import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/preact';
import { ReviewPanel } from './ReviewPanel';
import { RunsPanel } from './RunsPanel';
import { CredentialsPanel } from './CredentialsPanel';
import {
  reviewGroups,
  reviewFocusItemId,
  selectedBoardId,
  boards,
  boardRuns,
  boardCredentials,
  _resetBoardsForTest,
} from '../../store/boards';
import { serverMode } from '../../store/server-mode';
import type { Board, BoardRunSummary, StagedRecord } from '../../api/boards';

/** The SPA boots read-only so the demo never flashes mutation UI; these tests
 *  are about a real writable deploy, so flip it explicitly. */
function writable() {
  serverMode.value = {
    readOnly: false, authed: true, authMode: 'basic', demoShowAll: false,
  };
}

const realFetch = globalThis.fetch;

function mkRecord(over: Partial<StagedRecord> = {}): StagedRecord {
  const actions = over.actions ?? [
    {
      id: 'a1',
      action: 'comment',
      params: { body: 'Hi Dana — the refund was issued on the 3rd.' },
      preview: 'Hi Dana — the refund was issued on the 3rd.',
      writes: 1,
      state: 'pending' as const,
    },
  ];
  return {
    board_id: 'acme-jira',
    item_id: '46',
    item_key: 'SUP-812',
    item_title: 'Refund not received',
    item_url: 'https://acme.atlassian.net/browse/SUP-812',
    content_hash: 'hash-1',
    run_id: 'run-1-aaaa',
    state: 'pending',
    disposition: 'needs_review',
    reason: 'matched refund txn 8821 in Stripe; no further action needed',
    evidence: { tool_calls: 3, tokens: '12k' },
    actions,
    pending_actions: actions.filter((a) => a.state === 'pending'),
    open: true,
    decided_by: '',
    result: null,
    created_at: 1,
    updated_at: 1,
    ...over,
  };
}

function seedReview(records: StagedRecord[], disposition = 'needs_review') {
  boards.value = [
    {
      id: 'acme-jira',
      vendor: 'jira',
      display_name: 'Acme — Support',
      base_url: 'https://acme.atlassian.net',
      credential_ref: '@board-creds/JIRA_API_TOKEN',
      credential_set: true,
    } as Board,
  ];
  selectedBoardId.value = 'acme-jira';
  reviewGroups.value = [
    { disposition, count: records.length, items: records },
  ];
}

function stubFetch(status = 200, body: unknown = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  globalThis.fetch = vi.fn(async (url: unknown, init?: RequestInit) => {
    calls.push({
      url: String(url),
      method: init?.method ?? 'GET',
      body: init?.body ? JSON.parse(String(init.body)) : null,
    });
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => 'application/json' },
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

describe('/board Review', () => {
  beforeEach(() => {
    _resetBoardsForTest();
    writable();
    stubFetch(200, { groups: [], total: 0, open: 0 });
  });

  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('shows the proposed write, the reason and the evidence — not a transcript', () => {
    seedReview([mkRecord()]);
    render(<ReviewPanel />);
    expect(screen.getByText('comment')).toBeInTheDocument();
    expect(
      screen.getByText(/the refund was issued on the 3rd/),
    ).toBeInTheDocument();
    expect(screen.getByText(/matched refund txn 8821/)).toBeInTheDocument();
    expect(screen.getByText('tool calls')).toBeInTheDocument();
  });

  it('renders the proposed text verbatim, never as markup', () => {
    seedReview([
      mkRecord({
        actions: [
          {
            id: 'a1',
            action: 'comment',
            params: {},
            preview: '<img src=x onerror="alert(1)">',
            writes: 1,
            state: 'pending',
          },
        ],
        pending_actions: [
          {
            id: 'a1',
            action: 'comment',
            params: {},
            preview: '<img src=x onerror="alert(1)">',
            writes: 1,
            state: 'pending',
          },
        ],
      }),
    ]);
    const { container } = render(<ReviewPanel />);
    expect(container.querySelector('img')).toBeNull();
    expect(
      container.querySelector('.board-review-preview')?.textContent,
    ).toContain('<img src=x');
  });

  it('keeps the deep link out to the real ticket', () => {
    seedReview([mkRecord()]);
    render(<ReviewPanel />);
    const link = screen.getByText(/Open ticket/).closest('a');
    expect(link).toHaveAttribute(
      'href',
      'https://acme.atlassian.net/browse/SUP-812',
    );
    expect(link).toHaveAttribute('rel', 'noreferrer noopener');
  });

  it('sends the content_hash the CARD was drawn from, plus an approval id', async () => {
    seedReview([mkRecord()]);
    render(<ReviewPanel />);
    const calls = stubFetch(200, { replayed: false, result: { ok: true } });
    fireEvent.click(screen.getByText('Approve'));
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    const approve = calls.find((c) => c.url.includes('/approve'));
    expect(approve).toBeTruthy();
    const body = approve!.body as Record<string, string>;
    expect(body.content_hash).toBe('hash-1');
    expect(body.approval_id.length).toBeGreaterThanOrEqual(8);
  });

  it('a 409 surfaces the stale message and reloads the queue', async () => {
    seedReview([mkRecord()]);
    render(<ReviewPanel />);
    stubFetch(409, {
      error: 'the ticket changed on the board after this action was staged',
      code: 'stale',
    });
    fireEvent.click(screen.getByText('Approve'));
    await waitFor(() =>
      expect(screen.getByText(/the ticket changed on the board/)).toBeInTheDocument(),
    );
  });

  it('disables approve when nothing is staged', () => {
    seedReview([mkRecord({ actions: [], pending_actions: [] })]);
    render(<ReviewPanel />);
    expect(screen.getByText('Approve')).toBeDisabled();
  });

  it('a decided item shows its outcome and offers no buttons', () => {
    seedReview([
      mkRecord({ open: false, state: 'approved', decided_by: 'dashboard:me' }),
    ]);
    render(<ReviewPanel />);
    expect(screen.getByText(/approved/)).toBeInTheDocument();
    expect(screen.queryByText('Approve')).toBeNull();
    expect(screen.queryByText('Reject')).toBeNull();
  });

  it('a partial outcome says some writes failed rather than looking approved', () => {
    seedReview([mkRecord({ open: false, state: 'partial' })]);
    render(<ReviewPanel />);
    expect(screen.getByText(/some writes failed/)).toBeInTheDocument();
  });

  it('rejecting asks for a reason before it fires', async () => {
    seedReview([mkRecord()]);
    render(<ReviewPanel />);
    const calls = stubFetch(200, { replayed: false });
    fireEvent.click(screen.getByText('Reject'));
    // The dialog is a deliberate second step — one tap must not reject.
    expect(calls.length).toBe(0);
    expect(screen.getByText(/Nothing will be written/)).toBeInTheDocument();
  });

  it('editing opens a multiline box seeded with what the agent proposed', () => {
    seedReview([mkRecord()]);
    render(<ReviewPanel />);
    fireEvent.click(screen.getByText('Edit'));
    // PromptDialog renders through a Portal, so it is not inside `container`.
    const box = document.querySelector('textarea');
    expect(box).toBeTruthy();
    expect((box as HTMLTextAreaElement).value).toContain(
      'the refund was issued on the 3rd',
    );
  });

  it('prompts to pick a board first', () => {
    render(<ReviewPanel />);
    expect(screen.getByText(/Select a board/i)).toBeInTheDocument();
  });
});

describe('/board Runs', () => {
  beforeEach(() => {
    _resetBoardsForTest();
    writable();
    stubFetch(200, { runs: [] });
  });

  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  function mkRun(over: Partial<BoardRunSummary> = {}): BoardRunSummary {
    return {
      id: 'run-1-aaaa',
      board_id: 'acme-jira',
      mode: 'propose',
      status: 'running',
      concurrency: 3,
      requested_concurrency: 3,
      clamp_reason: '',
      created_at: 1,
      updated_at: 1,
      finished_at: null,
      error: '',
      listing_complete: true,
      truncation_reason: '',
      total: 6,
      counts: {
        pending: 3, claimed: 0, working: 1, done: 2, failed: 0, skipped: 0,
      },
      done: 2,
      failed: 0,
      skipped: 0,
      ...over,
    };
  }

  function seedRuns(runs: BoardRunSummary[]) {
    selectedBoardId.value = 'acme-jira';
    boardRuns.value = { 'acme-jira': runs };
  }

  it('shows progress against the total', () => {
    seedRuns([mkRun()]);
    render(<RunsPanel />);
    expect(screen.getByText('2/6')).toBeInTheDocument();
    expect(screen.getByText('running')).toBeInTheDocument();
  });

  it('says a run was CLAMPED rather than leaving it to be inferred', () => {
    seedRuns([
      mkRun({
        concurrency: 4,
        requested_concurrency: 20,
        clamp_reason: 'clamped to 4 from 20: KC_MAX_TASKS=12 with 8 already live',
      }),
    ]);
    render(<RunsPanel />);
    expect(screen.getByText('clamped')).toBeInTheDocument();
  });

  it('flags a run whose board listing was incomplete', () => {
    seedRuns([
      mkRun({
        listing_complete: false,
        truncation_reason: 'full_page_no_pagination_metadata',
      }),
    ]);
    render(<RunsPanel />);
    expect(screen.getByText('partial listing')).toBeInTheDocument();
  });

  it('warns before an autonomous run', () => {
    selectedBoardId.value = 'acme-jira';
    render(<RunsPanel />);
    fireEvent.input(screen.getByLabelText('Run mode'), {
      target: { value: 'autonomous' },
    });
    expect(
      screen.getByText(/writes to the board without asking/),
    ).toBeInTheDocument();
  });

  it('prompts to pick a board first', () => {
    render(<RunsPanel />);
    expect(screen.getByText(/Select a board/i)).toBeInTheDocument();
  });
});

describe('/board Credentials', () => {
  beforeEach(() => {
    _resetBoardsForTest();
    writable();
    stubFetch(200, { credentials: [] });
  });

  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('shows a last-4 hint and never a value', () => {
    boardCredentials.value = [
      {
        name: 'JIRA_API_TOKEN',
        format: 'basic',
        username: 'me@example.com',
        hint: '…abcd',
        created_at: 1,
        updated_at: 1,
      },
    ];
    const { container } = render(<CredentialsPanel />);
    expect(screen.getByText('JIRA_API_TOKEN')).toBeInTheDocument();
    expect(screen.getByText('…abcd')).toBeInTheDocument();
    // The hint is all the UI ever gets; there is no route that reads a value
    // back, so nothing resembling one can reach the DOM.
    expect(container.querySelector('.board-cred-hint')?.textContent).toBe('…abcd');
    expect(container.textContent).not.toContain('super-secret');
  });

  it('asks for a username only for the basic format', () => {
    render(<CredentialsPanel />);
    expect(screen.queryByText('Username')).toBeNull();
    fireEvent.input(screen.getByLabelText('Credential type'), {
      target: { value: 'basic' },
    });
    expect(screen.getByText('Username')).toBeInTheDocument();
    expect(screen.getByText(/composes the Basic header/)).toBeInTheDocument();
  });

  it('keeps the secret field out of autocomplete and out of the DOM value', () => {
    const { container } = render(<CredentialsPanel />);
    const secret = container.querySelector('input[type=password]');
    expect(secret).toHaveAttribute('autocomplete', 'off');
  });

  it('explains the reference form when nothing is stored', () => {
    render(<CredentialsPanel />);
    expect(screen.getByText('@board-creds/NAME')).toBeInTheDocument();
  });
});

describe('arriving for one card (#704)', () => {
  // A highlight alone left the card wherever the queue put it — often below
  // the fold. Arriving from a run item's outcome, a feed link or the waiting
  // badge has to put that card where the eye (and keyboard focus) lands.
  let scrolled: { id: string | null; behavior: unknown }[];
  const originalScroll = HTMLElement.prototype.scrollIntoView;
  const originalMatchMedia = window.matchMedia;

  function reduceMotion(on: boolean) {
    window.matchMedia = ((query: string) => ({
      matches: on && query.includes('prefers-reduced-motion'),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  }

  const three = () => [
    mkRecord({ item_id: '46', item_key: 'SUP-46' }),
    mkRecord({ item_id: '47', item_key: 'SUP-47' }),
    mkRecord({ item_id: '48', item_key: 'SUP-48' }),
  ];

  beforeEach(() => {
    _resetBoardsForTest();
    writable();
    scrolled = [];
    HTMLElement.prototype.scrollIntoView = function (
      this: HTMLElement,
      arg?: boolean | ScrollIntoViewOptions,
    ) {
      scrolled.push({
        id: this.getAttribute('data-item-id'),
        behavior: typeof arg === 'object' ? arg.behavior : undefined,
      });
    };
    reduceMotion(false);
  });

  afterEach(() => {
    HTMLElement.prototype.scrollIntoView = originalScroll;
    window.matchMedia = originalMatchMedia;
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
  });

  it('scrolls that card into view and moves focus to it', () => {
    const records = three();
    stubFetch(200, { groups: [{ disposition: 'needs_review', count: 3, items: records }] });
    seedReview(records);
    reviewFocusItemId.value = '47';

    const { container } = render(<ReviewPanel />);

    expect(scrolled.map((s) => s.id)).toEqual(['47']);
    expect(scrolled[0].behavior).toBe('smooth');
    const card = container.querySelector('[data-item-id="47"]');
    expect(document.activeElement).toBe(card);
    // Only the card someone was sent to joins the focus order.
    expect(container.querySelector('[data-item-id="46"]')?.getAttribute('tabindex')).toBeNull();
  });

  it('does not scroll when nobody was sent to a card', () => {
    const records = three();
    stubFetch(200, { groups: [{ disposition: 'needs_review', count: 3, items: records }] });
    seedReview(records);
    render(<ReviewPanel />);
    expect(scrolled).toEqual([]);
  });

  it('scrolls once per request, not on every refresh of the queue', async () => {
    const records = three();
    stubFetch(200, { groups: [{ disposition: 'needs_review', count: 3, items: records }] });
    seedReview(records);
    reviewFocusItemId.value = '47';
    render(<ReviewPanel />);

    // An event-driven refresh while the reviewer reads further down must not
    // drag the page back up to the card.
    await act(async () => {
      reviewGroups.value = [
        { disposition: 'needs_review', count: 3, items: three() },
      ];
    });
    expect(scrolled.map((s) => s.id)).toEqual(['47']);
  });

  it('scrolls again when sent to a different card', async () => {
    const records = three();
    stubFetch(200, { groups: [{ disposition: 'needs_review', count: 3, items: records }] });
    seedReview(records);
    reviewFocusItemId.value = '47';
    render(<ReviewPanel />);

    await act(async () => {
      reviewFocusItemId.value = '48';
    });
    expect(scrolled.map((s) => s.id)).toEqual(['47', '48']);
  });

  it('waits for a card that has not loaded yet, then scrolls to it', async () => {
    stubFetch(200, { groups: [] });
    seedReview([]);
    reviewGroups.value = [];
    reviewFocusItemId.value = '48';
    render(<ReviewPanel />);
    expect(scrolled).toEqual([]);

    await act(async () => {
      reviewGroups.value = [
        { disposition: 'needs_review', count: 3, items: three() },
      ];
    });
    expect(scrolled.map((s) => s.id)).toEqual(['48']);
  });

  it('jumps instead of animating for someone who asked for reduced motion', () => {
    reduceMotion(true);
    const records = three();
    stubFetch(200, { groups: [{ disposition: 'needs_review', count: 3, items: records }] });
    seedReview(records);
    reviewFocusItemId.value = '46';
    render(<ReviewPanel />);
    expect(scrolled).toEqual([{ id: '46', behavior: 'auto' }]);
  });

  it('finds a card whose id is not a plain number', () => {
    // GitHub GraphQL global ids carry colons, which a hand-built CSS selector
    // would have to escape.
    const records = [
      mkRecord({ item_id: 'I_kwDOA:4102', item_key: '4102' }),
      mkRecord({ item_id: '47', item_key: 'SUP-47' }),
    ];
    stubFetch(200, { groups: [{ disposition: 'needs_review', count: 2, items: records }] });
    seedReview(records);
    reviewFocusItemId.value = 'I_kwDOA:4102';
    render(<ReviewPanel />);
    expect(scrolled.map((s) => s.id)).toEqual(['I_kwDOA:4102']);
  });
});
