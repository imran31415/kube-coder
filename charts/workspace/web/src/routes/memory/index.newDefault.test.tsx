import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { MemoryRoute } from './index';
import { projects, selectedProjectId, _resetProjectsForTest } from '../../store/projects';
import { drawerOpen } from '../../store/ui';
import { serverMode } from '../../store/server-mode';

/**
 * A new memory is filed under the active project's namespace (#358).
 *
 * This is the difference between per-project memory being real and being a
 * convention nobody follows: retrieval is namespace-scoped (#359), so anything
 * written under `user.` while working in a project is invisible to that
 * project's own chats.
 */

// The graph pane pulls in d3 and needs layout; the drawer under test doesn't.
vi.mock('./MemoryGraph', () => ({ MemoryGraph: () => null }));

function payloadFor(url: string): unknown {
  if (url.includes('/api/projects')) {
    return {
      projects: [
        {
          id: 'kc',
          name: 'kube-coder',
          memory_namespace: 'project.kc',
          workdirs: [],
          status: 'active',
        },
      ],
    };
  }
  if (url.includes('/api/memory')) return { memories: [] };
  return {};
}

const realFetch = globalThis.fetch;

const realMode = serverMode.value;

beforeEach(() => {
  localStorage.clear();
  _resetProjectsForTest();
  drawerOpen.value = null;
  // The New-memory button is mutator-gated; the store defaults to read-only
  // until the mode probe lands.
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  globalThis.fetch = vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => payloadFor(String(url)),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  serverMode.value = realMode;
  localStorage.clear();
  _resetProjectsForTest();
  drawerOpen.value = null;
});

/** The New-memory drawer's Namespace field. */
async function openNewMemory(): Promise<HTMLInputElement> {
  fireEvent.click(screen.getByRole('button', { name: /New memory/ }));
  const labels = await screen.findAllByText('Namespace');
  const field = labels[0].closest('label') as HTMLElement;
  return field.querySelector('input') as HTMLInputElement;
}

describe('new-memory namespace default', () => {
  it('is user. outside a project', async () => {
    render(<MemoryRoute />);
    await waitFor(() => expect(projects.value).toHaveLength(1));
    expect((await openNewMemory()).value).toBe('user.');
  });

  it("is the active project's namespace inside one", async () => {
    render(<MemoryRoute />);
    await waitFor(() => expect(projects.value).toHaveLength(1));
    selectedProjectId.value = 'kc';
    expect((await openNewMemory()).value).toBe('project.kc');
  });

  it('still lets the user type any namespace before saving', async () => {
    render(<MemoryRoute />);
    await waitFor(() => expect(projects.value).toHaveLength(1));
    selectedProjectId.value = 'kc';
    const input = await openNewMemory();
    fireEvent.input(input, { target: { value: 'user.preferences' } });
    expect(input.value).toBe('user.preferences');
  });
});
