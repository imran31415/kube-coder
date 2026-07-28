import { render, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { Chat } from './Chat';
import { events, activeThreadId, activeStatus, sending, config } from '../../store/hypervisor';
import type { HvEvent } from './transcript';

// Consecutive tool chips fold into one group chip (#546). The grouping rules
// themselves are unit-tested in transcript.test.ts; this covers the rendering
// contract: one chip per run, children only once expanded, and a failed run
// never hiding itself.

const realFetch = globalThis.fetch;
let seq = 0;

function call(name: string, input: unknown): HvEvent {
  seq += 1;
  return { seq, ts: seq, role: 'assistant', type: 'tool_call', tool_id: `t${seq}`, tool: { name, input } };
}
function result(text: string, isError = false): HvEvent {
  seq += 1;
  return { seq, ts: seq, role: 'system', type: 'tool_result', tool_use_id: `t${seq - 1}`, text, is_error: isError };
}
function ran(cmd: string, isError = false): HvEvent[] {
  return [call('Bash', { command: cmd }), result(isError ? 'boom' : 'ok', isError)];
}

const chips = (c: Element) => c.querySelectorAll('.hv-activity:not(.hv-activity-group)');
const groups = (c: Element) => c.querySelectorAll('.hv-activity-group');

beforeEach(() => {
  seq = 0;
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({}),
  })) as unknown as typeof fetch;
  config.value = {
    enabled: true,
    defaultAssistant: 'claude',
    workdir: '/home/dev',
    readOnly: false,
    assistants: [],
  };
  activeThreadId.value = 't1';
  activeStatus.value = 'idle';
  sending.value = false;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  events.value = [];
  activeThreadId.value = null;
  config.value = null;
});

describe('Chat activity grouping (#546)', () => {
  it('renders one chip for a run of 4 and reveals every call when expanded', () => {
    events.value = [
      { seq: 0, ts: 0, role: 'user', type: 'message', text: 'go' },
      ...ran('git status'),
      ...ran('npm test'),
      ...ran('ls'),
      ...ran('pwd'),
    ];
    const { container } = render(<Chat />);

    expect(groups(container)).toHaveLength(1);
    expect(chips(container)).toHaveLength(0); // children hidden while collapsed
    const head = container.querySelector('.hv-activity-group .hv-activity-head') as HTMLElement;
    expect(head.textContent).toContain('Ran 4 commands');
    expect(head.getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(head);
    expect(head.getAttribute('aria-expanded')).toBe('true');
    expect(chips(container)).toHaveLength(4);
    // Each child is still individually expandable to its own detail.
    fireEvent.click(chips(container)[0].querySelector('.hv-activity-head') as HTMLElement);
    expect((container.querySelector('.hv-activity-detail') as HTMLElement).textContent).toContain('git status');

    fireEvent.click(head);
    expect(chips(container)).toHaveLength(0);
  });

  it('leaves a run of two rendering exactly as before', () => {
    events.value = [
      { seq: 0, ts: 0, role: 'user', type: 'message', text: 'go' },
      ...ran('git status'),
      ...ran('npm test'),
    ];
    const { container } = render(<Chat />);
    expect(groups(container)).toHaveLength(0);
    expect(chips(container)).toHaveLength(2);
  });

  it('starts a failed run expanded, error-styled, and counts the failure', () => {
    events.value = [
      { seq: 0, ts: 0, role: 'user', type: 'message', text: 'go' },
      ...ran('git status'),
      ...ran('npm test', true),
      ...ran('ls'),
    ];
    const { container } = render(<Chat />);

    const group = container.querySelector('.hv-activity-group') as HTMLElement;
    expect(group.classList.contains('is-error')).toBe(true);
    expect(group.textContent).toContain('Ran 3 commands · 1 failed');
    expect(chips(container)).toHaveLength(3); // starts expanded…
    fireEvent.click(group.querySelector('.hv-activity-head') as HTMLElement);
    expect(chips(container)).toHaveLength(0); // …but the user can still collapse it
  });

  it('surfaces the newest call on the trailing group while the agent works', () => {
    activeStatus.value = 'running';
    events.value = [
      { seq: 0, ts: 0, role: 'user', type: 'message', text: 'go' },
      ...ran('git status'),
      ...ran('npm test'),
      call('Read', { file_path: '/home/dev/app.ts' }),
    ];
    const { container } = render(<Chat />);
    const head = container.querySelector('.hv-activity-group .hv-activity-head') as HTMLElement;
    expect(head.textContent).toContain('Ran 3 tools');
    expect(head.querySelector('.hv-activity-hint')?.textContent).toContain('Read file');
  });
});
