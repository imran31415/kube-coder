import { describe, it, expect } from 'vitest';
import {
  groupActivity,
  turnWindow,
  TURN_WINDOW,
  TURN_WINDOW_STEP,
  type HvBlock,
  type HvRenderBlock,
} from './hvTranscript';

describe('turnWindow (mobile)', () => {
  it('hides nothing when the thread fits in the window', () => {
    expect(turnWindow(0, TURN_WINDOW)).toEqual({ start: 0, hidden: 0 });
    expect(turnWindow(TURN_WINDOW, TURN_WINDOW)).toEqual({ start: 0, hidden: 0 });
  });

  it('renders only the tail once the thread exceeds the window', () => {
    const total = TURN_WINDOW + 9;
    expect(turnWindow(total, TURN_WINDOW)).toEqual({ start: 9, hidden: 9 });
  });

  it('reveals another page when visible grows by a step', () => {
    const total = TURN_WINDOW * 3;
    const next = turnWindow(total, TURN_WINDOW + TURN_WINDOW_STEP);
    expect(next.hidden).toBe(total - TURN_WINDOW - TURN_WINDOW_STEP);
  });

  it('never overshoots the total and clamps a tiny visible up to the floor', () => {
    expect(turnWindow(10, 999)).toEqual({ start: 0, hidden: 0 });
    expect(turnWindow(TURN_WINDOW + 4, 1)).toEqual({ start: 4, hidden: 4 });
  });
});

describe('groupActivity (mobile, #546)', () => {
  const act = (label: string, detail = '', error?: boolean): HvBlock => ({
    kind: 'activity',
    label,
    detail,
    ...(error ? { error: true } : {}),
  });

  it('is a no-op for an empty list and for turns with no activity', () => {
    expect(groupActivity([])).toEqual([]);
    const prose: HvBlock[] = [{ kind: 'prose', text: 'hi' }, { kind: 'embed', port: 3000 }];
    expect(groupActivity(prose)).toEqual(prose);
  });

  it('leaves a run shorter than GROUP_MIN untouched', () => {
    const blocks: HvBlock[] = [act('Ran command', 'ls'), act('Ran command', 'pwd')];
    expect(groupActivity(blocks)).toEqual(blocks);
  });

  it('folds a run of GROUP_MIN+ into one group, preserving order', () => {
    const blocks: HvBlock[] = [act('Ran command', 'a'), act('Ran command', 'b'), act('Ran command', 'c')];
    const out = groupActivity(blocks);
    expect(out).toHaveLength(1);
    const g = out[0] as Extract<HvRenderBlock, { kind: 'activity_group' }>;
    expect(g.kind).toBe('activity_group');
    expect(g.label).toBe('Ran 3 commands');
    expect(g.errors).toBe(0);
    expect(g.items.map((b) => b.detail)).toEqual(['a', 'b', 'c']);
  });

  it('breaks the run on prose and keeps narrative order', () => {
    const blocks: HvBlock[] = [
      act('Ran command', 'a'),
      act('Ran command', 'b'),
      act('Ran command', 'c'),
      { kind: 'prose', text: 'now the answer' },
      act('Read file', 'x'),
      act('Read file', 'y'),
      act('Read file', 'z'),
      act('Read file', 'w'),
    ];
    const out = groupActivity(blocks);
    expect(out.map((b) => b.kind)).toEqual(['activity_group', 'prose', 'activity_group']);
    expect((out[0] as { label: string }).label).toBe('Ran 3 commands');
    expect((out[2] as { label: string }).label).toBe('Read 4 files');
  });

  it('pluralises known labels and falls back to ×N for unknown (MCP) ones', () => {
    const label = (l: string, n: number) =>
      (groupActivity(Array.from({ length: n }, () => act(l)))[0] as { label: string }).label;
    expect(label('Wrote file', 3)).toBe('Wrote 3 files');
    expect(label('Edited file', 5)).toBe('Edited 5 files');
    expect(label('Searched', 4)).toBe('Searched 4 times');
    expect(label('Ran a task', 3)).toBe('Ran 3 tasks');
    expect(label('Fetched a page', 3)).toBe('Fetched 3 pages');
    expect(label('show app preview', 4)).toBe('show app preview ×4');
  });

  it('summarises a mixed run as "Ran N tools"', () => {
    const blocks: HvBlock[] = [act('Ran command'), act('Read file'), act('Edited file')];
    expect((groupActivity(blocks)[0] as { label: string }).label).toBe('Ran 3 tools');
  });

  it('counts failures and never hides them', () => {
    const blocks: HvBlock[] = [act('Ran command', 'a'), act('Ran command', 'b', true), act('Ran command', 'c')];
    const g = groupActivity(blocks)[0] as Extract<HvRenderBlock, { kind: 'activity_group' }>;
    expect(g.errors).toBe(1);
    expect(g.label).toBe('Ran 3 commands · 1 failed');
  });
});
