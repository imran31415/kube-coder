import { act, cleanup, render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { CtoRoute } from './index';
import { ChatHistory } from './ChatHistory';
import { CTO_RAIL_COLLAPSED_KEY, CTO_BRIEF_COLLAPSED_KEY } from './railSplit';
import { _resetProjectsForTest } from '../../store/projects';
import { activeThreadId, threads, deletedThreads, closeThread, sending, setChatContext, sendMessage, refreshThreads, refreshDeletedThreads } from '../../store/hypervisor';
import type { HypervisorThread } from '../../api/hypervisor';
import { justOnboarded } from '../../store/onboarding';
import { claudeReady, claudeProbed } from '../../store/claude';

// The route mounts several async loaders (config, threads, discover, projects).
// Feed them all benign JSON so nothing rejects; we only assert the deterministic
// first-paint (masthead + Workspace scope + starter chips) which needs no data.
function payloadFor(url: string): unknown {
  if (url.includes('/api/hypervisor/config')) {
    return { enabled: true, assistants: [], defaultAssistant: 'claude', workdir: '/home/dev' };
  }
  if (url.includes('/api/hypervisor/threads')) return { threads: [] };
  if (url.includes('/api/projects/_discover')) return { candidates: [], registered: [] };
  if (url.includes('/api/projects')) return { projects: [] };
  // Claude readiness (#494/#500): connected by default, so the welcome path
  // under test is the chips one. The keyless-gate test flips this.
  if (url.includes('/api/subscriptions')) return { subscriptions: {}, claude_ready: claudeReadyResponse };
  return {};
}

/** What the readiness probe reports for the test in flight. */
let claudeReadyResponse = true;

const realFetch = globalThis.fetch;

beforeEach(() => {
  _resetProjectsForTest();
  localStorage.clear();
  claudeProbed.value = false;
  claudeReadyResponse = true;
  globalThis.fetch = vi.fn(async (url: string) => ({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => payloadFor(String(url)),
  })) as unknown as typeof fetch;
});
afterEach(async () => {
  // Let mount-time loaders settle before unmounting their signal subscribers.
  await act(async () => {});
  cleanup();
  globalThis.fetch = realFetch;
  localStorage.clear();
  closeThread();
  setChatContext('', null);
  _resetProjectsForTest();
  justOnboarded.value = false;
  claudeReady.value = null;
  claudeProbed.value = false;
});

describe('CtoRoute', () => {
  it('renders the deterministic first-paint: masthead, Workspace scope, starter chips', () => {
    render(<CtoRoute />);
    expect(screen.getByText('AI CTO')).toBeTruthy();
    // Named by role so the assertion holds in both rail states (#530) — the
    // test viewport (1024px) sits in the auto-collapse band.
    expect(screen.getByRole('button', { name: /Workspace/ })).toBeTruthy();
    // Starter chips render before any LLM call / thread exists (lazy creation).
    expect(screen.getByText('What should I focus on?')).toBeTruthy();
    expect(screen.getByText('What are we building?')).toBeTruthy();
  });

  it('shows the first-win opener + build chips when routed straight from onboarding (#487)', () => {
    justOnboarded.value = true;
    claudeReady.value = true; // enterCto only routes here once Claude is ready
    render(<CtoRoute />);
    expect(screen.getByText(/Tell me in one sentence what you'd like to build/)).toBeTruthy();
    expect(screen.getByText('Portfolio site')).toBeTruthy();
    // The normal returning-user chips are replaced, not shown alongside.
    expect(screen.queryByText('What should I focus on?')).toBeNull();
    // The one-shot flag is consumed on mount so a later visit is normal.
    expect(justOnboarded.value).toBe(false);
  });
});

// ── First-win hero composition + keyless chip-flash (#500) ───────────────────
describe('CtoRoute first-win hero (#500)', () => {
  it('composes the welcome into the transcript centring slot, not above the chat', () => {
    const { container } = render(<CtoRoute />);
    // The hero lives inside the transcript's centring host, so opener → chips →
    // composer read as one vertically-centred unit.
    expect(container.querySelector('.hv-welcome-host .cto-welcome')).toBeTruthy();
    // …and no longer as a sibling pinned to the top of the chat column.
    expect(container.querySelector('.cto-main > .cto-welcome')).toBeNull();
  });

  it('hosts the keyless connect gate in the same slot', async () => {
    claudeReadyResponse = false;
    const { container } = render(<CtoRoute />);
    await waitFor(() =>
      expect(container.querySelector('.hv-welcome-host .cto-connect')).toBeTruthy(),
    );
    expect(container.querySelector('.cto-chip')).toBeNull();
  });

  it('holds the chips inert until the readiness probe settles, then releases them', async () => {
    render(<CtoRoute />);
    // Probe still in flight: the chips render (no layout jump) but can't fire a
    // build that would die keyless before the connect gate swaps in.
    expect((screen.getByText('What should I focus on?') as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() =>
      expect((screen.getByText('What should I focus on?') as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
  });

  it('wears the shared route masthead so CTO/Feed/Mission match (#510)', () => {
    render(<CtoRoute />);
    expect(screen.getByText('AI CTO').classList.contains('route-title')).toBe(true);
  });
});

// ── Collapsible side panes (#530) ────────────────────────────────────────────
// The test viewport is 1024px wide — inside the auto-collapse band (≤1200px)
// but above the 860px stacked breakpoint, i.e. exactly the band the issue is
// about, so these exercise the heuristic and the explicit override.
describe('CtoRoute side panes (#530)', () => {
  it('auto-collapses both panes in the cramped band, giving the chat the width', () => {
    const { container } = render(<CtoRoute />);
    expect(container.querySelector('.cto-rail-collapsed')).toBeTruthy();
    expect(container.querySelector('.cto-brief-tab')).toBeTruthy();
    // No expanded brief and no split handle while the rail is a strip.
    expect(container.querySelector('.cto-brief')).toBeNull();
    expect(container.querySelector('.cto-split-handle')).toBeNull();
    // The chat keeps the whole middle track: rail strip + chat + brief tab.
    const root = container.querySelector('.route-cto') as HTMLElement;
    expect(root.style.gridTemplateColumns).toBe('48px minmax(0, 1fr) 34px');
  });

  it('an explicit expand overrides the heuristic and persists', () => {
    const { container } = render(<CtoRoute />);
    fireEvent.click(screen.getByRole('button', { name: 'Expand projects rail' }));
    fireEvent.click(screen.getByRole('button', { name: /Show project brief/ }));

    expect(container.querySelector('.cto-rail-collapsed')).toBeNull();
    expect(container.querySelector('.cto-brief')).toBeTruthy();
    expect(container.querySelector('.cto-split-handle')).toBeTruthy();
    expect(localStorage.getItem(CTO_RAIL_COLLAPSED_KEY)).toBe('0');
    expect(localStorage.getItem(CTO_BRIEF_COLLAPSED_KEY)).toBe('0');
  });

  it('restores a persisted choice on the next visit, heuristic notwithstanding', () => {
    localStorage.setItem(CTO_RAIL_COLLAPSED_KEY, '0');
    localStorage.setItem(CTO_BRIEF_COLLAPSED_KEY, '0');
    const { container } = render(<CtoRoute />);
    expect(container.querySelector('.cto-rail-collapsed')).toBeNull();
    expect(container.querySelector('.cto-brief')).toBeTruthy();
    expect(container.querySelector('.cto-brief-tab')).toBeNull();
  });

  it('stands the brief aside on the first send, but not when the user pinned it open', async () => {
    // Pretend a wide desktop so nothing is auto-collapsed by width.
    const realMM = window.matchMedia;
    window.matchMedia = ((q: string) =>
      ({
        matches: false,
        media: q,
        addEventListener: () => {},
        removeEventListener: () => {},
      })) as unknown as typeof window.matchMedia;
    try {
      const { container } = render(<CtoRoute />);
      expect(container.querySelector('.cto-brief')).toBeTruthy();

      sending.value = true;
      await waitFor(() => expect(container.querySelector('.cto-brief-tab')).toBeTruthy());

      // Re-opening it is an explicit choice: a later send leaves it alone.
      fireEvent.click(screen.getByRole('button', { name: /Show project brief/ }));
      sending.value = false;
      sending.value = true;
      await waitFor(() => expect(container.querySelector('.cto-brief')).toBeTruthy());
      expect(container.querySelector('.cto-brief-tab')).toBeNull();
    } finally {
      sending.value = false;
      window.matchMedia = realMM;
    }
  });

  it('collapsing an expanded pane persists the collapsed choice too', () => {
    localStorage.setItem(CTO_RAIL_COLLAPSED_KEY, '0');
    localStorage.setItem(CTO_BRIEF_COLLAPSED_KEY, '0');
    const { container } = render(<CtoRoute />);
    fireEvent.click(screen.getByRole('button', { name: 'Collapse projects rail' }));
    fireEvent.click(screen.getByRole('button', { name: 'Collapse project brief' }));
    expect(localStorage.getItem(CTO_RAIL_COLLAPSED_KEY)).toBe('1');
    expect(localStorage.getItem(CTO_BRIEF_COLLAPSED_KEY)).toBe('1');
    expect(container.querySelector('.cto-rail-collapsed')).toBeTruthy();
    expect(container.querySelector('.cto-brief-tab')).toBeTruthy();
  });
});


describe('CTO conversation management', () => {
  let live: HypervisorThread[];
  let trash: HypervisorThread[];
  let failDelete: boolean;
  let failRestore: boolean;
  let latestStatus: string;
  let calls: { path: string; method: string; body?: Record<string, unknown> }[];
  const row = (id: string, project_id = ''): HypervisorThread => ({
    id, title: id, persona: 'cto', project_id, assistant: 'claude', status: 'idle', created_at: 1, updated_at: 2,
  });

  beforeEach(() => {
    live = [row('first'), row('second')];
    trash = [];
    failDelete = false;
    failRestore = false;
    latestStatus = 'idle';
    calls = [];
    sending.value = false;
    globalThis.fetch = vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost');
      const path = url.pathname.replace(/^\/oauth/, '');
      const method = init?.method || 'GET';
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ path, method, body });
      let status = 200;
      let data: unknown = payloadFor(path);
      const match = path.match(/^\/api\/hypervisor\/threads\/([^/]+)(?:\/(restore|rename|messages))?$/);
      if (path === '/api/hypervisor/threads' && method === 'GET') {
        // Deliberately return mixed data: the UI/store must enforce its scope.
        data = { threads: url.searchParams.has('deleted') ? trash : live };
      } else if (path === '/api/hypervisor/threads' && method === 'POST') {
        const t = { ...row('created', String(body.project_id || '')), persona: body.persona };
        live = [t, ...live]; data = { thread: t };
      } else if (match) {
        const [, id, action] = match;
        const t = [...live, ...trash].find(t => t.id === id) || row(id);
        if (method === 'DELETE') {
          if (failDelete) { status = 500; data = { error: 'Archive failed' }; }
          else { live = live.filter(t => t.id !== id); trash.push({ ...t, deleted_at: 10 }); data = { ok: true }; }
        } else if (action === 'restore') {
          if (failRestore) { status = 500; data = { error: 'Restore failed' }; }
          else { trash = trash.filter(t => t.id !== id); live.push({ ...t, deleted_at: null }); data = { ok: true, restored: true }; }
        } else if (action === 'rename') {
          live = live.map(t => t.id === id ? { ...t, title: String(body.title) } : t);
          data = { thread: { ...t, title: body.title } };
        } else { data = { thread: { ...t, status: latestStatus }, events: [] }; }
      }
      return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
    }) as typeof fetch;
  });

  async function mount() {
    render(<CtoRoute />);
    await waitFor(() => expect(activeThreadId.value).toBe('first'));
  }
  async function history() {
    fireEvent.click(screen.getByRole('button', { name: 'Chat history' }));
    await screen.findByRole('button', { name: 'Archive first' });
  }
  async function confirmArchive(title = 'first') {
    await history();
    fireEvent.click(screen.getByRole('button', { name: `Archive ${title}` }));
    fireEvent.click(await screen.findByRole('button', { name: 'Archive', exact: true }));
  }

  it('New chat opens an empty composer and the first send creates a separate CTO thread', async () => {
    await mount();
    fireEvent.input(screen.getByRole('textbox', { name: 'Message Kube-Coder' }), { target: { value: 'Unsent draft' } });
    fireEvent.click(screen.getByRole('button', { name: 'New chat', exact: true }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message Kube-Coder' })).toHaveValue(''));
    expect(activeThreadId.value).toBeNull();
    expect(live.map(t => t.id)).toContain('first');
    expect(calls.some(c => c.method === 'DELETE')).toBe(false);
    await sendMessage('A fresh conversation');
    expect(activeThreadId.value).toBe('created');
    expect(calls.find(c => c.method === 'POST' && c.path === '/api/hypervisor/threads')?.body?.persona).toBe('cto');
  });

  it('opens another conversation from history', async () => {
    await mount(); await history();
    fireEvent.click(screen.getByRole('button', { name: /second.*ago/ }));
    await waitFor(() => expect(activeThreadId.value).toBe('second'));
  });

  it('confirms archive, selects the next chat, and offers Undo', async () => {
    await mount(); await confirmArchive();
    await waitFor(() => expect(activeThreadId.value).toBe('second'));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    await waitFor(() => expect(trash).toHaveLength(0));
    expect(live.map(t => t.id)).toContain('first');
    expect(activeThreadId.value).toBe('second');
  });

  it('archiving the last chat leaves a fresh composer', async () => {
    live = [row('first')];
    await mount(); await confirmArchive();
    await waitFor(() => expect(activeThreadId.value).toBeNull());
    expect(await screen.findByRole('button', { name: 'Undo' })).toBeTruthy();
  });

  it('archiving an inactive chat keeps the current selection', async () => {
    await mount(); await confirmArchive('second');
    await screen.findByRole('button', { name: 'Undo' });
    expect(activeThreadId.value).toBe('first');
  });

  it('cancelling confirmation never sends DELETE', async () => {
    await mount(); await history();
    fireEvent.click(screen.getByRole('button', { name: 'Archive first' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel', exact: true }));
    expect(calls.some(c => c.method === 'DELETE')).toBe(false);
    expect(activeThreadId.value).toBe('first');
  });

  it('rechecks running status after confirmation', async () => {
    await mount(); await history();
    fireEvent.click(screen.getByRole('button', { name: 'Archive first' }));
    latestStatus = 'running';
    fireEvent.click(await screen.findByRole('button', { name: 'Archive', exact: true }));
    await screen.findByText(/This chat is running/);
    expect(calls.some(c => c.method === 'DELETE')).toBe(false);
    expect(activeThreadId.value).toBe('first');
  });

  it('failed archive keeps the active chat and never offers Undo', async () => {
    failDelete = true;
    await mount(); await confirmArchive();
    await screen.findByText('Archive failed');
    expect(activeThreadId.value).toBe('first');
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
  });

  it('renames a chat through the existing endpoint', async () => {
    await mount(); await history();
    fireEvent.click(screen.getByRole('button', { name: 'Rename first' }));
    fireEvent.input(await screen.findByRole('textbox', { name: 'Rename chat' }), { target: { value: 'Planning next week' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }));
    await waitFor(() => expect(live[0].title).toBe('Planning next week'));
    expect(activeThreadId.value).toBe('first');
  });

  it('restores from Recently deleted and keeps failed restores available', async () => {
    trash = [{ ...row('archived'), deleted_at: 10 }];
    await mount(); await history();
    fireEvent.click(screen.getByRole('button', { name: 'Recently deleted' }));
    failRestore = true;
    fireEvent.click(await screen.findByRole('button', { name: 'Restore archived' }));
    await screen.findByText('Restore failed');
    expect(trash).toHaveLength(1);
    failRestore = false;
    fireEvent.click(screen.getByRole('button', { name: 'Restore archived' }));
    await waitFor(() => expect(trash).toHaveLength(0));
    expect(activeThreadId.value).toBe('first');
  });

  it('filters live and deleted history by project and persona, including Workspace', async () => {
    live = [row('a', 'A'), row('b', 'B'), row('root'), { ...row('plain', 'A'), persona: '' }];
    trash = live.map(t => ({ ...t, deleted_at: 10 }));
    setChatContext('cto', 'A');
    await refreshThreads(); await refreshDeletedThreads();
    expect(threads.value.map(t => t.id)).toEqual(['a']);
    expect(deletedThreads.value.map(t => t.id)).toEqual(['a']);
    setChatContext('cto', 'B');
    await refreshThreads(); expect(threads.value.map(t => t.id)).toEqual(['b']);
    setChatContext('cto', null);
    await refreshThreads(); expect(threads.value.map(t => t.id)).toEqual(['root']);
    setChatContext('', null);
    await refreshThreads(); expect(threads.value.map(t => t.id)).toEqual(['plain']);
  });

  it('shows restore failures inside the narrow history sheet', async () => {
    trash = [{ ...row('archived'), deleted_at: 10 }];
    failRestore = true;
    setChatContext('cto', null);
    await refreshThreads();
    render(<ChatHistory narrow />);
    fireEvent.click(screen.getByRole('button', { name: 'Chat history' }));
    fireEvent.click(screen.getByRole('button', { name: 'Recently deleted' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Restore archived' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Restore failed');
    expect(screen.getByRole('dialog', { name: 'Chat history' }).contains(alert)).toBe(true);
  });

  it('archiving a chat never mutates its project or brief', async () => {
    await mount();
    calls = [];
    await confirmArchive();
    await screen.findByRole('button', { name: 'Undo' });
    expect(calls.filter(c => c.path.startsWith('/api/projects') && c.method !== 'GET')).toEqual([]);
  });
});
