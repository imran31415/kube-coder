import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/preact';
import { RunsPanel } from './RunsPanel';
import {
  selectedBoardId,
  boards,
  runFormFor,
  setRunForm,
  _resetRunFormsForTest,
} from '../../store/boards';
import { serverMode } from '../../store/server-mode';
import type { Board } from '../../api/boards';

/**
 * Runs form persistence (#643).
 *
 * Leaving the Runs tab unmounts this panel, and watching a run means going
 * to Review and back. When Items / At once lived in component state, that
 * round trip silently restored the heavier defaults (10 @ 3) under an
 * operator who had chosen 5 @ 2 — and the next Start run spent real pod
 * memory on a run nobody asked for.
 */

const realFetch = globalThis.fetch;

function mkBoard(id: string): Board {
  return {
    id,
    vendor: 'github',
    display_name: id,
    base_url: 'https://github.com',
    credential_ref: '@board-creds/GITHUB_TOKEN',
    credential_set: true,
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetRunFormsForTest();
  // The form lives inside <MutatorOnly>, which hides itself until the mode
  // probe says the deploy is writable.
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  boards.value = [mkBoard('board-a'), mkBoard('board-b')];
  selectedBoardId.value = 'board-a';
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ runs: [], strategies: {}, orders: [] }),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  globalThis.fetch = realFetch;
  localStorage.clear();
  _resetRunFormsForTest();
  selectedBoardId.value = null;
  boards.value = [];
});

function items() {
  return screen.getByLabelText('Items') as HTMLInputElement;
}
function atOnce() {
  return screen.getByLabelText('At once') as HTMLInputElement;
}

describe('RunsPanel form persistence', () => {
  it('keeps Items / At once across an unmount (tab switch)', () => {
    const first = render(<RunsPanel />);
    fireEvent.input(items(), { target: { value: '5' } });
    fireEvent.input(atOnce(), { target: { value: '2' } });
    expect(items().value).toBe('5');

    // Switch to Review and back — index.tsx renders the tabs conditionally,
    // so the panel is destroyed, not hidden.
    first.unmount();
    render(<RunsPanel />);

    expect(items().value).toBe('5');
    expect(atOnce().value).toBe('2');
  });

  it('keeps Mode across an unmount', () => {
    const first = render(<RunsPanel />);
    fireEvent.input(screen.getByLabelText('Run mode'), {
      target: { value: 'autonomous' },
    });
    first.unmount();
    render(<RunsPanel />);
    expect((screen.getByLabelText('Run mode') as HTMLSelectElement).value).toBe(
      'autonomous',
    );
  });

  it('survives a full page reload via localStorage', () => {
    const first = render(<RunsPanel />);
    fireEvent.input(items(), { target: { value: '7' } });
    fireEvent.input(atOnce(), { target: { value: '4' } });
    first.unmount();

    // A reload rebuilds the store from storage alone.
    _resetRunFormsForTest();
    render(<RunsPanel />);

    expect(items().value).toBe('7');
    expect(atOnce().value).toBe('4');
  });

  it('does not carry one board\'s numbers over to another', () => {
    const first = render(<RunsPanel />);
    fireEvent.input(items(), { target: { value: '5' } });
    fireEvent.input(atOnce(), { target: { value: '2' } });
    first.unmount();

    selectedBoardId.value = 'board-b';
    const second = render(<RunsPanel />);
    expect(items().value).toBe('10');
    expect(atOnce().value).toBe('3');
    second.unmount();

    // …and board A still remembers its own.
    selectedBoardId.value = 'board-a';
    render(<RunsPanel />);
    expect(items().value).toBe('5');
    expect(atOnce().value).toBe('2');
  });

  it('leaves the submitted values on the form after Start run', async () => {
    render(<RunsPanel />);
    fireEvent.input(items(), { target: { value: '5' } });
    fireEvent.input(atOnce(), { target: { value: '2' } });
    fireEvent.submit(screen.getByText('Start run').closest('form')!);
    await Promise.resolve();
    expect(items().value).toBe('5');
    expect(atOnce().value).toBe('2');
  });
});

describe('run form storage is untrusted', () => {
  it('clamps hand-edited values back into the input bounds', () => {
    localStorage.setItem(
      'kc.boardRunForm',
      JSON.stringify({ 'board-a': { limit: 9999, concurrency: 99, mode: 'propose' } }),
    );
    _resetRunFormsForTest();
    expect(runFormFor('board-a')).toMatchObject({ limit: 500, concurrency: 8 });

    localStorage.setItem(
      'kc.boardRunForm',
      JSON.stringify({ 'board-a': { limit: 0, concurrency: -3 } }),
    );
    _resetRunFormsForTest();
    expect(runFormFor('board-a')).toMatchObject({ limit: 1, concurrency: 1 });
  });

  it('falls back to defaults for junk, a bad mode, or unparseable storage', () => {
    localStorage.setItem(
      'kc.boardRunForm',
      JSON.stringify({ 'board-a': { limit: 'lots', concurrency: null, mode: 'yolo' } }),
    );
    _resetRunFormsForTest();
    expect(runFormFor('board-a')).toEqual({
      mode: 'propose',
      limit: 10,
      concurrency: 3,
      strategy: '',
    });

    localStorage.setItem('kc.boardRunForm', 'not json');
    _resetRunFormsForTest();
    expect(runFormFor('board-a')).toEqual({
      mode: 'propose',
      limit: 10,
      concurrency: 3,
      strategy: '',
    });
  });

  it('clamps on write too, so a stray value never reaches startRun', () => {
    setRunForm('board-a', { limit: 100000, concurrency: 64 });
    expect(runFormFor('board-a')).toMatchObject({ limit: 500, concurrency: 8 });
  });

  it('ignores writes with no board selected', () => {
    setRunForm(null, { limit: 42 });
    expect(runFormFor(null)).toMatchObject({ limit: 10, concurrency: 3 });
  });
});
