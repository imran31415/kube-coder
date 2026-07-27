import { render, screen } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { CtoRoute } from './index';
import { _resetProjectsForTest } from '../../store/projects';
import { closeThread, setChatContext } from '../../store/hypervisor';
import { justOnboarded } from '../../store/onboarding';
import { claudeReady } from '../../store/claude';

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
  return {};
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  _resetProjectsForTest();
  globalThis.fetch = vi.fn(async (url: string) => ({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => payloadFor(String(url)),
  })) as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  closeThread();
  setChatContext('', null);
  _resetProjectsForTest();
  justOnboarded.value = false;
  claudeReady.value = null;
});

describe('CtoRoute', () => {
  it('renders the deterministic first-paint: masthead, Workspace scope, starter chips', () => {
    render(<CtoRoute />);
    expect(screen.getByText('AI CTO')).toBeTruthy();
    expect(screen.getByText('Workspace')).toBeTruthy();
    // Starter chips render before any LLM call / thread exists (lazy creation).
    expect(screen.getByText('What should I focus on?')).toBeTruthy();
    expect(screen.getByText('What are we building?')).toBeTruthy();
  });

  it('shows the first-win opener + build chips when routed straight from onboarding (#487)', () => {
    justOnboarded.value = true;
    claudeReady.value = true; // enterCto only routes here once Claude is ready
    render(<CtoRoute />);
    expect(screen.getByText(/Tell me in one sentence what you'd like to build/)).toBeTruthy();
    expect(screen.getByText('Build me a personal portfolio website')).toBeTruthy();
    // The normal returning-user chips are replaced, not shown alongside.
    expect(screen.queryByText('What should I focus on?')).toBeNull();
    // The one-shot flag is consumed on mount so a later visit is normal.
    expect(justOnboarded.value).toBe(false);
  });
});
