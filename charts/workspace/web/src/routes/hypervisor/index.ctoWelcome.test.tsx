import { render, screen, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { HypervisorRoute } from './index';
import {
  closeThread,
  newChatMode,
  selectedProject,
  threads as threadStore,
} from '../../store/hypervisor';
import { _resetProjectsForTest } from '../../store/projects';
import { claudeProbed, claudeReady } from '../../store/claude';
import { ctoHandoff } from '../../store/feed';
import { justOnboarded } from '../../store/onboarding';
import { serverMode } from '../../store/server-mode';
import { navigate } from '../../store/router';

/**
 * The AI CTO's front door, now inside Chat (#683).
 *
 * Three things used to live only on the /cto page and had to survive the
 * page's deletion: the deterministic welcome + starter chips, the first-win
 * opener a just-onboarded user sees (#487), and the Feed's "Discuss with CTO"
 * handoff (#470). None of them needed a route — they needed a mode.
 */

let createdThreads: unknown[] = [];
const realFetch = globalThis.fetch;
const realMode = serverMode.value;

function payloadFor(url: string, init?: RequestInit): unknown {
  if (url.includes('/api/hypervisor/config')) {
    return {
      enabled: true,
      assistants: [{ id: 'claude', label: 'Claude Code' }],
      defaultAssistant: 'claude',
      workdir: '/home/dev',
    };
  }
  if (url.includes('/api/hypervisor/threads')) {
    if (init?.method === 'POST') {
      createdThreads.push(JSON.parse(String(init.body)));
      return { thread: { id: 'new', title: 'new', assistant: 'claude', status: 'idle' } };
    }
    return { threads: [] };
  }
  if (url.includes('/api/projects')) {
    return { projects: [{ id: 'kc', name: 'kube-coder', memory_namespace: 'project.kc', workdirs: [] }] };
  }
  if (url.includes('/api/workspace/dirs')) return { dirs: [] };
  // The route probes Claude readiness on mount (#494). Without this the probe
  // would land mid-test and flip the welcome into its connect gate.
  if (url.includes('/api/subscriptions')) {
    return { subscriptions: {}, claude_ready: claudeReady.value !== false };
  }
  return {};
}

beforeEach(() => {
  createdThreads = [];
  localStorage.clear();
  _resetProjectsForTest();
  threadStore.value = [];
  newChatMode.value = '';
  selectedProject.value = '';
  ctoHandoff.value = null;
  justOnboarded.value = false;
  claudeReady.value = true;
  claudeProbed.value = true;
  serverMode.value = { ...realMode, readOnly: false, ctoEnabled: true };
  navigate('/hypervisor');
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => payloadFor(String(url), init),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  serverMode.value = realMode;
  localStorage.clear();
  closeThread();
  newChatMode.value = '';
  selectedProject.value = '';
  ctoHandoff.value = null;
  justOnboarded.value = false;
  _resetProjectsForTest();
  navigate('/hypervisor');
});

describe('the CTO welcome in Chat (#683)', () => {
  it('is not shown for a plain new chat', async () => {
    render(<HypervisorRoute />);
    await screen.findByLabelText('Chat agent');
    await waitFor(() =>
      expect(screen.queryByText('What should I focus on?')).toBeNull(),
    );
  });

  it('opens with the deterministic starter chips in CTO mode', async () => {
    newChatMode.value = 'cto';
    render(<HypervisorRoute />);
    expect(await screen.findByText('What should I focus on?')).toBeTruthy();
    expect(screen.getByText('Break a goal into tasks')).toBeTruthy();
  });

  it('names the project the chat will be filed into', async () => {
    newChatMode.value = 'cto';
    selectedProject.value = 'kc';
    render(<HypervisorRoute />);
    expect(await screen.findByText('Where is kube-coder at?')).toBeTruthy();
  });

  it('swaps in the first-win build prompts straight out of onboarding (#487)', async () => {
    newChatMode.value = 'cto';
    justOnboarded.value = true;
    render(<HypervisorRoute />);
    expect(await screen.findByText('Portfolio site')).toBeTruthy();
    // One-shot: a later visit in the same session gets the normal welcome.
    await waitFor(() => expect(justOnboarded.value).toBe(false));
  });

  it('offers the connect panel instead of chips with no Claude credential (#494)', async () => {
    newChatMode.value = 'cto';
    claudeReady.value = false;
    render(<HypervisorRoute />);
    await waitFor(() =>
      expect(screen.queryByText('What should I focus on?')).toBeNull(),
    );
    expect(
      screen.getByText(/Connect Claude and I'll start building/),
    ).toBeTruthy();
  });

  it('keeps the chips inert until the credential probe settles (#500)', async () => {
    // The signal starts null, so a keyless user would otherwise get live build
    // chips for the length of the probe — and a tap in that window fires a
    // build that can only die on a raw provider error.
    newChatMode.value = 'cto';
    claudeProbed.value = false;
    render(<HypervisorRoute />);
    const chip = (await screen.findByText('What should I focus on?')) as HTMLButtonElement;
    expect(chip.disabled).toBe(true);
  });
});

describe('the Feed "Discuss with CTO" handoff (#470) lands in Chat', () => {
  it('creates a CTO chat filed into the item’s project, with the prefix sent', async () => {
    ctoHandoff.value = { projectId: 'kc', text: 'Re: Release is the bottleneck' };
    render(<HypervisorRoute />);

    await waitFor(() => expect(createdThreads).toHaveLength(1));
    expect(createdThreads[0]).toMatchObject({
      message: 'Re: Release is the bottleneck',
      persona: 'cto',
      project_id: 'kc',
    });
    // Consumed, so a later visit doesn't re-send it.
    expect(ctoHandoff.value).toBeNull();
  });
});
