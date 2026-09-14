import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { describe, expect, it, afterEach, beforeEach, vi } from 'vitest';
import { DevcontainerPanel } from './DevcontainerPanel';
import { DevcontainerApplyDialog } from './DevcontainerApplyDialog';
import {
  devcontainers,
  openApplyDialog,
  _resetDevcontainerForTest,
} from '../../store/devcontainer';
import { serverMode } from '../../store/server-mode';
import type { DevcontainerRecord } from '../../api/devcontainer';

/**
 * Component tests for #594. The assertions that matter are about DISCLOSURE:
 *   * the blocking-unsupported block is rendered EXPANDED, not tucked into a
 *     <details>, because someone who never opens it believes their `features`
 *     installed;
 *   * the apply dialog renders the command text VERBATIM — that string is what
 *     the user is consenting to run;
 *   * the needs-root warning is inline with the command that carries it;
 *   * the hash the panel loaded with is the hash the apply sends.
 */

const emptyLifecycle = () => ({
  onCreate: [], updateContent: [], postCreate: [], postStart: [],
});

function mk(over: Partial<DevcontainerRecord> = {}): DevcontainerRecord {
  return {
    workdir: '/home/dev/api', found: true, path: '/home/dev/api/.devcontainer/devcontainer.json',
    rel_path: '.devcontainer/devcontainer.json', error: '', error_line: 0, error_column: 0,
    config_hash: 'hash-1', name: 'Node 20 + Postgres', workspace_folder: '/home/dev/api',
    lifecycle: emptyLifecycle(),
    ports: [{ port: 3000, label: 'API' }],
    ports_skipped: [], extensions: ['ms-python.python'], extensions_rejected: [],
    settings: { 'editor.formatOnSave': true }, settings_denied: [],
    env: { NODE_ENV: 'development' }, env_denied: [],
    unsupported: [], caveats: [], needs_root: false,
    applied: {
      ports_pinned: [], extensions_installed: [], settings_written: {}, env: {},
      applied_at: null, auto_apply: false, consented_hash: '',
    },
    lifecycle_status: {}, busy: false,
    ...over,
  } as DevcontainerRecord;
}

function seed(rec: DevcontainerRecord) {
  devcontainers.value = { [rec.workdir]: rec };
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  _resetDevcontainerForTest();
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  // The panel loads on mount; answer with whatever is already seeded so the
  // effect does not blank the store mid-assertion.
  globalThis.fetch = vi.fn(async (u: string) => {
    const wd = decodeURIComponent(new URL(u, 'http://x').searchParams.get('workdir') ?? '');
    const body = devcontainers.value[wd] ?? { found: false };
    return {
      ok: true, status: 200, statusText: '',
      headers: { get: () => 'application/json' },
      json: async () => body, text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
  _resetDevcontainerForTest();
});

describe('DevcontainerPanel', () => {
  it('renders nothing when the workdir has no devcontainer', () => {
    const { container } = render(<DevcontainerPanel workdir="/home/dev/plain" />);
    expect(container.querySelector('.dc-section')).toBeNull();
  });

  it('renders the name and the declared counts', () => {
    seed(mk());
    render(<DevcontainerPanel workdir="/home/dev/api" />);
    expect(screen.getByText('Node 20 + Postgres')).toBeTruthy();
    expect(screen.getByText(/1 ports/)).toBeTruthy();
    expect(screen.getByText(/1 extensions/)).toBeTruthy();
  });

  it('renders per-hook status pills', () => {
    seed(mk({
      lifecycle: {
        ...emptyLifecycle(),
        postCreate: [{ name: '', kind: 'shell', command: 'npm ci', display: 'npm ci', needs_root: false, root_reasons: [], caveats: [] }],
      },
      lifecycle_status: {
        postCreate: { status: 'pending', count: 1 },
        postStart: { status: 'done', count: 1, ran_at: Math.floor(Date.now() / 1000) - 120 },
      },
    }));
    render(<DevcontainerPanel workdir="/home/dev/api" />);
    expect(screen.getByText('not run')).toBeTruthy();
    expect(screen.getByText('ran')).toBeTruthy();
    expect(screen.getByText('2m ago')).toBeTruthy();
  });

  it('shows blocking unsupported properties EXPANDED, with reason and remedy', () => {
    seed(mk({
      unsupported: [{
        key: 'features', severity: 'blocking',
        detail: 'ghcr.io/devcontainers/features/node:1',
        reason: 'Dev container features install as root at image-build time.',
        remedy: 'Move it to postCreateCommand.',
      }],
    }));
    const { container } = render(<DevcontainerPanel workdir="/home/dev/api" />);
    // Not inside a <details> — a collapsed warning is a warning nobody reads.
    const block = container.querySelector('.dc-unsupported-blocking');
    expect(block).toBeTruthy();
    expect(block?.closest('details')).toBeNull();
    expect(screen.getByText('features')).toBeTruthy();
    expect(screen.getByText(/install as root/)).toBeTruthy();
    expect(screen.getByText(/postCreateCommand/)).toBeTruthy();
  });

  it('collapses non-blocking unsupported properties', () => {
    seed(mk({
      unsupported: [{
        key: 'initializeCommand', severity: 'ignored', detail: '',
        reason: 'Runs on the client.', remedy: '',
      }],
    }));
    const { container } = render(<DevcontainerPanel workdir="/home/dev/api" />);
    expect(container.querySelector('details')).toBeTruthy();
    expect(container.querySelector('.dc-unsupported-blocking')).toBeNull();
  });

  it('reports an unreadable file instead of pretending it is absent', () => {
    seed(mk({ error: 'invalid JSON at line 12, column 3' }));
    render(<DevcontainerPanel workdir="/home/dev/api" />);
    expect(screen.getByText(/invalid JSON at line 12/)).toBeTruthy();
  });

  it('hides the Apply button in a read-only workspace', () => {
    serverMode.value = { readOnly: true, authed: true, authMode: 'basic', demoShowAll: false };
    seed(mk());
    render(<DevcontainerPanel workdir="/home/dev/api" />);
    expect(screen.queryByRole('button', { name: /Apply/ })).toBeNull();
    expect(screen.getByText(/Read-only workspace/)).toBeTruthy();
  });

  it('disables Apply while a run is in progress', () => {
    seed(mk({ busy: true }));
    render(<DevcontainerPanel workdir="/home/dev/api" />);
    const btn = screen.getByRole('button', { name: /Running/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });
});

describe('DevcontainerApplyDialog', () => {
  const withPostCreate = () => mk({
    lifecycle: {
      ...emptyLifecycle(),
      postCreate: [{
        name: '', kind: 'shell',
        command: 'sudo apt-get install -y libpq-dev && npm ci',
        display: 'sudo apt-get install -y libpq-dev && npm ci',
        needs_root: true, root_reasons: ['`sudo` requires root'], caveats: [],
      }],
    },
    needs_root: true,
  });

  it('renders the command text verbatim', () => {
    seed(withPostCreate());
    openApplyDialog('/home/dev/api');
    render(<DevcontainerApplyDialog workdir="/home/dev/api" />);
    expect(screen.getByText('sudo apt-get install -y libpq-dev && npm ci')).toBeTruthy();
  });

  it('renders the needs-root warning inline with the command', () => {
    seed(withPostCreate());
    openApplyDialog('/home/dev/api');
    render(<DevcontainerApplyDialog workdir="/home/dev/api" />);
    expect(screen.getByText(/needs root/)).toBeTruthy();
    expect(screen.getByText(/UID 1000 with no privilege escalation/)).toBeTruthy();
  });

  it('repeats blocking properties above the command list', () => {
    seed(mk({
      ...withPostCreate(),
      unsupported: [{
        key: 'features', severity: 'blocking', detail: '',
        reason: 'features install as root at image-build time.', remedy: '',
      }],
    }));
    openApplyDialog('/home/dev/api');
    render(<DevcontainerApplyDialog workdir="/home/dev/api" />);
    // Modal renders through a Portal, so the dialog is in document.body, not
    // in the render container.
    const blockingEl = document.querySelector('.dc-unsupported-blocking');
    const cmdEl = document.querySelector('.dc-cmd');
    expect(blockingEl).toBeTruthy();
    expect(cmdEl).toBeTruthy();
    expect(blockingEl!.compareDocumentPosition(cmdEl!) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
  });

  it('defaults every hook to OFF, so the safe action is one click', () => {
    seed(withPostCreate());
    openApplyDialog('/home/dev/api');
    render(<DevcontainerApplyDialog workdir="/home/dev/api" />);
    const box = document.querySelector('input[type=checkbox]') as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(screen.getByText('Apply without running commands')).toBeTruthy();
  });

  it('sends the hash it was loaded with once a hook is ticked', async () => {
    seed(withPostCreate());
    openApplyDialog('/home/dev/api');
    const posts: unknown[] = [];
    globalThis.fetch = vi.fn(async (_u: string, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'POST') {
        posts.push(JSON.parse(init!.body as string));
        return {
          ok: true, status: 202, statusText: '',
          headers: { get: () => 'application/json' },
          json: async () => ({
            ports_pinned: [], port_conflicts: [], settings_written: [],
            settings_skipped: [], extensions_installed: [], extension_failures: [],
            env: [], hooks_started: ['postCreate'], unsupported: [],
          }),
          text: async () => '{}',
        } as unknown as Response;
      }
      return {
        ok: true, status: 200, statusText: '',
        headers: { get: () => 'application/json' },
        json: async () => devcontainers.value['/home/dev/api'],
        text: async () => '{}',
      } as unknown as Response;
    }) as unknown as typeof fetch;

    render(<DevcontainerApplyDialog workdir="/home/dev/api" />);
    fireEvent.click(document.querySelector('input[type=checkbox]')!);
    fireEvent.click(screen.getByText(/Apply and run 1 hook/));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect((posts[0] as { config_hash: string }).config_hash).toBe('hash-1');
    expect((posts[0] as { hooks: string[] }).hooks).toEqual(['postCreate']);
  });

  it('offers the boot opt-in only once postStart is selected', () => {
    seed(mk({
      lifecycle: {
        ...emptyLifecycle(),
        postStart: [{ name: '', kind: 'shell', command: 'npm start', display: 'npm start', needs_root: false, root_reasons: [], caveats: [] }],
      },
    }));
    openApplyDialog('/home/dev/api');
    render(<DevcontainerApplyDialog workdir="/home/dev/api" />);
    expect(document.querySelector('.dc-auto')).toBeNull();
    fireEvent.click(document.querySelector('input[type=checkbox]')!);
    expect(document.querySelector('.dc-auto')).toBeTruthy();
  });

  it('renders nothing for an unreadable file', () => {
    seed(mk({ error: 'broken' }));
    openApplyDialog('/home/dev/api');
    render(<DevcontainerApplyDialog workdir="/home/dev/api" />);
    expect(document.querySelector('.dc-dialog-title')).toBeNull();
  });
});
