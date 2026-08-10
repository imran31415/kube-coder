import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/preact';
import { BoardRail } from './BoardRail';
import { ItemList } from './ItemList';
import { ItemDetail } from './ItemDetail';
import {
  boards,
  boardItems,
  selectedBoardId,
  selectedItemId,
  _resetBoardsForTest,
} from '../../store/boards';
import { visibleNavGroups } from '../../store/router';
import type { Board, BoardItem } from '../../api/boards';

const realFetch = globalThis.fetch;

function mkBoard(over: Partial<Board> = {}): Board {
  return {
    id: 'acme-jira',
    vendor: 'jira',
    display_name: 'Acme — Support',
    base_url: 'https://acme.atlassian.net',
    credential_ref: '@board-creds/JIRA_API_TOKEN',
    credential_set: true,
    ...over,
  };
}

function mkItem(over: Partial<BoardItem> = {}): BoardItem {
  return {
    id: '46',
    key: 'SUP-5',
    ref: {},
    title: 'Refund not received',
    body: 'Dana says the refund never arrived.',
    status: { normalized: 'IN_PROGRESS', raw: 'In Review' },
    priority: { normalized: 'HIGH', raw: 'P2' },
    assignee: { name: 'Sam' },
    contact: { name: 'Dana', email: 'dana@example.com' },
    collection: { name: 'Support' },
    tags: ['billing'],
    url: 'https://acme.atlassian.net/browse/SUP-5',
    created_at: '',
    updated_at: '2026-08-01',
    raw: {},
    ...over,
  };
}

function seed(items: BoardItem[], over = {}) {
  boards.value = [mkBoard()];
  selectedBoardId.value = 'acme-jira';
  boardItems.value = {
    'acme-jira': {
      items,
      complete: true,
      truncation_reason: '',
      pages_fetched: 1,
      ...over,
    },
  };
}

describe('/board', () => {
  beforeEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => ({ boards: [] }),
      text: async () => '{}',
    })) as unknown as typeof fetch;
  });

  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('renders a board card in the rail', () => {
    boards.value = [mkBoard()];
    render(<BoardRail />);
    expect(screen.getByText('Acme — Support')).toBeInTheDocument();
    expect(screen.getByText('jira')).toBeInTheDocument();
  });

  it('flags a board whose credential no longer resolves', () => {
    boards.value = [mkBoard({ credential_set: false })];
    render(<BoardRail />);
    expect(screen.getByText('needs key')).toBeInTheDocument();
  });

  it('explains what a board is when none are connected', () => {
    boards.value = [];
    render(<BoardRail />);
    expect(screen.getByText(/No boards connected/i)).toBeInTheDocument();
  });

  it('lists items with their normalized status', () => {
    seed([mkItem()]);
    render(<ItemList />);
    expect(screen.getByText('SUP-5')).toBeInTheDocument();
    expect(screen.getByText('Refund not received')).toBeInTheDocument();
    expect(screen.getByText('in progress')).toBeInTheDocument();
  });

  it('renders an UNMAPPED status as the vendor wrote it, not as an error', () => {
    seed([mkItem({ status: { normalized: null, raw: 'Pending Customer' } })]);
    const { container } = render(<ItemList />);
    expect(screen.getByText('Pending Customer')).toBeInTheDocument();
    expect(container.querySelector('.board-status-raw')).toBeTruthy();
  });

  it('states plainly when a listing may be incomplete', () => {
    seed([mkItem()], {
      complete: false,
      truncation_reason: 'full_page_no_pagination_metadata',
    });
    render(<ItemList />);
    expect(screen.getByText(/Possibly incomplete/i)).toBeInTheDocument();
    expect(screen.getByText(/may be more items/i)).toBeInTheDocument();
  });

  it('says "complete" in the footer only when it is', () => {
    seed([mkItem()]);
    const { container } = render(<ItemList />);
    expect(container.querySelector('.board-items-foot')?.textContent).toContain(
      'complete',
    );
  });

  it('prompts to pick a board before one is selected', () => {
    render(<ItemList />);
    expect(screen.getByText(/Select a board/i)).toBeInTheDocument();
  });

  it('shows the deep link out to the real ticket', () => {
    seed([mkItem()]);
    selectedItemId.value = '46';
    render(<ItemDetail />);
    const link = screen.getByText(/Open in jira/i).closest('a');
    expect(link).toHaveAttribute('href', 'https://acme.atlassian.net/browse/SUP-5');
    expect(link).toHaveAttribute('rel', 'noreferrer noopener');
  });

  it('shows the vendor status alongside the normalized bucket', () => {
    seed([mkItem()]);
    selectedItemId.value = '46';
    render(<ItemDetail />);
    expect(screen.getByText('Vendor status')).toBeInTheDocument();
    expect(screen.getByText('In Review')).toBeInTheDocument();
  });

  it('surfaces the external requester as a first-class field', () => {
    seed([mkItem()]);
    selectedItemId.value = '46';
    render(<ItemDetail />);
    expect(screen.getByText('Requester')).toBeInTheDocument();
    expect(screen.getByText(/dana@example.com/)).toBeInTheDocument();
  });

  it('marks the item text as data, not instructions', () => {
    seed([mkItem()]);
    selectedItemId.value = '46';
    render(<ItemDetail />);
    expect(screen.getByText(/data, not instructions/i)).toBeInTheDocument();
  });

  it('renders the body as plain text, never as markup', () => {
    seed([mkItem({ body: '<img src=x onerror="alert(1)">' })]);
    selectedItemId.value = '46';
    const { container } = render(<ItemDetail />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('.board-detail-body pre')?.textContent).toContain(
      '<img src=x',
    );
  });
});

describe('/board nav gating', () => {
  it('is listed under Mission Control by default', () => {
    const groups = visibleNavGroups({});
    const mission = groups.find((g) => g.id === 'mission');
    expect(mission?.items.map((i) => i.path)).toContain('/board');
  });

  it('hides when boardEnabled is false', () => {
    const groups = visibleNavGroups({ boardEnabled: false });
    expect(groups.flatMap((g) => g.items.map((i) => i.path))).not.toContain(
      '/board',
    );
  });

  it('is independent of the CTO gate in both directions', () => {
    const noCto = visibleNavGroups({ ctoEnabled: false });
    const paths = noCto.flatMap((g) => g.items.map((i) => i.path));
    expect(paths).toContain('/board');
    expect(paths).not.toContain('/cto');

    const noBoard = visibleNavGroups({ boardEnabled: false });
    const paths2 = noBoard.flatMap((g) => g.items.map((i) => i.path));
    expect(paths2).toContain('/cto');
    expect(paths2).not.toContain('/board');
  });
});
