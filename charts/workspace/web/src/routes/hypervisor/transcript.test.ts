import { describe, it, expect } from 'vitest';
import { buildTurns, type HvEvent } from './transcript';

function ev(partial: Partial<HvEvent> & Pick<HvEvent, 'role' | 'type'>, seq: number): HvEvent {
  return { seq, ts: seq, ...partial } as HvEvent;
}

describe('buildTurns', () => {
  it('returns nothing for an empty event list', () => {
    expect(buildTurns([])).toEqual([]);
  });

  it('groups a user turn then an agent prose turn', () => {
    const turns = buildTurns([
      ev({ role: 'user', type: 'message', text: 'hi' }, 1),
      ev({ role: 'assistant', type: 'message', text: 'Everything looks healthy.' }, 2),
    ]);
    expect(turns).toHaveLength(2);
    expect(turns[0]).toEqual({ role: 'user', text: 'hi' });
    expect(turns[1].role).toBe('agent');
    if (turns[1].role === 'agent') {
      expect(turns[1].blocks[0]).toEqual({ kind: 'prose', text: 'Everything looks healthy.' });
    }
  });

  it('renders a tool call as a friendly activity chip', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 't1',
           tool: { name: 'Bash', input: { command: 'ps -eo pid,pcpu' } } }, 1),
    ]);
    expect(turns).toHaveLength(1);
    if (turns[0].role === 'agent') {
      const b = turns[0].blocks[0];
      expect(b.kind).toBe('activity');
      if (b.kind === 'activity') {
        expect(b.label).toBe('Ran command');
        expect(b.detail).toContain('ps -eo');
      }
    }
  });

  it('folds a tool_result into the preceding tool call', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 't1',
           tool: { name: 'Bash', input: { command: 'echo hi' } } }, 1),
      ev({ role: 'system', type: 'tool_result', tool_use_id: 't1', text: 'hi' }, 2),
    ]);
    if (turns[0].role === 'agent') {
      expect(turns[0].blocks).toHaveLength(1);
      const b = turns[0].blocks[0];
      if (b.kind === 'activity') expect(b.detail).toContain('hi');
    }
  });

  it('surfaces an error event as an error activity', () => {
    const turns = buildTurns([
      ev({ role: 'system', type: 'error', text: 'claude exited with code 1' }, 1),
    ]);
    if (turns[0].role === 'agent') {
      const b = turns[0].blocks[0];
      expect(b.kind).toBe('activity');
      if (b.kind === 'activity') {
        expect(b.error).toBe(true);
        expect(b.detail).toMatch(/exited with code 1/);
      }
    }
  });

  it('ignores status events (no chat content)', () => {
    const turns = buildTurns([ev({ role: 'system', type: 'status', status: 'idle' }, 1)]);
    expect(turns).toEqual([]);
  });

  it('maps an MCP tool name to its tool part', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 't1',
           tool: { name: 'mcp__dashboard__get_metrics', input: {} } }, 1),
    ]);
    if (turns[0].role === 'agent') {
      const b = turns[0].blocks[0];
      if (b.kind === 'activity') expect(b.label).toBe('get metrics');
    }
  });
});
