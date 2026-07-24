import { describe, it, expect, beforeEach } from 'vitest';
import {
  rememberLastSession,
  lastSessionId,
  forgetLastSession,
  restoreTarget,
} from './lastSession';

describe('lastSession', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('remembers and recalls per kind', () => {
    rememberLastSession('hypervisor', 'thread-a');
    rememberLastSession('build', 'task-1');
    expect(lastSessionId('hypervisor')).toBe('thread-a');
    expect(lastSessionId('build')).toBe('task-1');
  });

  it('returns null when nothing is remembered', () => {
    expect(lastSessionId('hypervisor')).toBeNull();
  });

  it('overwrites the previous id for the same kind', () => {
    rememberLastSession('hypervisor', 'a');
    rememberLastSession('hypervisor', 'b');
    expect(lastSessionId('hypervisor')).toBe('b');
  });

  it('forgets only when the id matches', () => {
    rememberLastSession('hypervisor', 'a');
    forgetLastSession('hypervisor', 'other');
    expect(lastSessionId('hypervisor')).toBe('a');
    forgetLastSession('hypervisor', 'a');
    expect(lastSessionId('hypervisor')).toBeNull();
  });

  describe('restoreTarget', () => {
    it('prefers the remembered id while it still exists', () => {
      rememberLastSession('hypervisor', 'b');
      expect(restoreTarget('hypervisor', ['a', 'b', 'c'])).toBe('b');
    });

    it('falls back to the newest (first) id when the remembered one is gone', () => {
      rememberLastSession('hypervisor', 'deleted');
      expect(restoreTarget('hypervisor', ['a', 'b'])).toBe('a');
    });

    it('falls back to the newest id when nothing is remembered', () => {
      expect(restoreTarget('build', ['t2', 't1'])).toBe('t2');
    });

    it('returns null for an empty list', () => {
      rememberLastSession('build', 'x');
      expect(restoreTarget('build', [])).toBeNull();
    });

    it('keeps kinds independent', () => {
      rememberLastSession('build', 'b1');
      expect(restoreTarget('hypervisor', ['h1', 'h2'])).toBe('h1');
    });
  });
});
