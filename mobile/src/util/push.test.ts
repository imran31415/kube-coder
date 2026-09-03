import { describe, it, expect } from 'vitest';
import { pushTargetFromData } from './push';

describe('pushTargetFromData', () => {
  it('maps a task ref to the task target', () => {
    expect(pushTargetFromData({ ref: 'task:42' })).toEqual({ kind: 'task', id: '42' });
  });

  it('maps a thread ref to the thread target', () => {
    expect(pushTargetFromData({ ref: 'thread:abc' })).toEqual({ kind: 'thread', id: 'abc' });
  });

  it('maps a memory ref to the memory target', () => {
    expect(pushTargetFromData({ ref: 'memory:user.foo' })).toEqual({ kind: 'memory' });
  });

  it('returns none for an empty or missing ref (caller falls back to the Feed)', () => {
    expect(pushTargetFromData({ ref: '' })).toEqual({ kind: 'none' });
    expect(pushTargetFromData({})).toEqual({ kind: 'none' });
    expect(pushTargetFromData(null)).toEqual({ kind: 'none' });
    expect(pushTargetFromData(undefined)).toEqual({ kind: 'none' });
  });

  it('returns none for an unrecognized ref kind', () => {
    expect(pushTargetFromData({ ref: 'bogus:1' })).toEqual({ kind: 'none' });
  });

  it('trims surrounding whitespace before resolving', () => {
    expect(pushTargetFromData({ ref: '  task:7  ' })).toEqual({ kind: 'task', id: '7' });
  });
});
