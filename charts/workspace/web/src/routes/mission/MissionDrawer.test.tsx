import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { MissionDrawer } from './MissionDrawer';
import { serverMode } from '../../store/server-mode';
import type { MissionCard, MissionDetail } from '../../api/mission';

const now = Math.floor(Date.now() / 1000);

function card(over: Partial<MissionCard>): MissionCard {
  return {
    id: 'build:t1', ref_id: 't1', kind: 'build', state: 'running',
    title: 'a build', headline: 'doing things',
    assistant: 'claude', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'kc/issue-425',
    created_at: now - 60, updated_at: now, finished_at: null,
    waiting_since: null, waiting_prompt: null, outcome: null,
    parent_id: null, children: [],
    ...over,
  };
}

function detail(over: Partial<MissionDetail> = {}): MissionDetail {
  return {
    card: card({}),
    timeline: [
      { at: now - 60, kind: 'start', text: 'Started', detail: 'fix the bug', link: null, status: 'ok' },
      {
        at: now - 30, kind: 'subagent', text: 'Spawned sub-agent — task t2',
        detail: 'explore', link: 'subagent:t2', status: 'ok',
      },
    ],
    output_tail: 'All tests green',
    ...over,
  };
}

/** URL-aware fetch stub: GET detail returns `payload`; every request is
 *  recorded so tests can assert on the POSTs the composer fires. */
function stubFetch(payload: MissionDetail) {
  const calls: { url: string; init?: RequestInit }[] = [];
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return {
      ok: true, status: 200,
      headers: { get: () => 'application/json' },
      json: async () => (String(url).includes('/missioncontrol/cards/') ? payload : {}),
    };
  }) as unknown as typeof fetch;
  return calls;
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic' };
});
afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
});

describe('MissionDrawer', () => {
  it('renders nothing fetched when closed', () => {
    const calls = stubFetch(detail());
    render(<MissionDrawer cardId={null} onClose={() => {}} />);
    expect(calls).toHaveLength(0);
  });

  it('loads and renders timeline, output tail and composer for a live build', async () => {
    stubFetch(detail());
    render(<MissionDrawer cardId="build:t1" onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('Started')).toBeInTheDocument();
    });
    // Timeline cross-link renders as a button; output tail and composer show.
    expect(screen.getByRole('button', { name: 'Spawned sub-agent — task t2' })).toBeInTheDocument();
    expect(screen.getByText('All tests green')).toBeInTheDocument();
    expect(screen.getByLabelText('Follow-up message')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open full session' })).toBeInTheDocument();
  });

  it('sends a follow-up to the task message endpoint', async () => {
    const calls = stubFetch(detail());
    render(<MissionDrawer cardId="build:t1" onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByLabelText('Follow-up message')).toBeInTheDocument();
    });
    fireEvent.input(screen.getByLabelText('Follow-up message'), {
      target: { value: 'also update the docs' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => {
      expect(calls.some(
        (c) => c.url.includes('/api/claude/tasks/t1/message') && c.init?.method === 'POST',
      )).toBe(true);
    });
  });

  it('sends chat follow-ups to the hypervisor thread endpoint', async () => {
    const chat = card({ id: 'chat:h1', ref_id: 'h1', kind: 'chat', state: 'done' });
    const calls = stubFetch(detail({ card: chat, output_tail: '' }));
    render(<MissionDrawer cardId="chat:h1" onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByLabelText('Follow-up message')).toBeInTheDocument();
    });
    fireEvent.input(screen.getByLabelText('Follow-up message'), {
      target: { value: 'resume and summarize' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => {
      expect(calls.some(
        (c) => c.url.includes('/api/hypervisor/threads/h1/messages') && c.init?.method === 'POST',
      )).toBe(true);
    });
  });

  it('hides the composer for a finished build', async () => {
    const done = card({ state: 'review', finished_at: now - 100, outcome: { ok: true, detail: 'completed' } });
    stubFetch(detail({ card: done }));
    render(<MissionDrawer cardId="build:t1" onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('Started')).toBeInTheDocument();
    });
    expect(screen.queryByLabelText('Follow-up message')).toBeNull();
  });

  it('shows an error when the card has aged off the board', async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: false, status: 404,
      headers: { get: () => 'application/json' },
      json: async () => ({ error: 'Card not found' }),
    })) as unknown as typeof fetch;
    render(<MissionDrawer cardId="build:gone" onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });
  });
});
