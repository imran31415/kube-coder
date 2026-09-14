import { describe, expect, it } from 'vitest';
import type { HypervisorThread } from '../api/types';
import { ctoThreads, selectionAfterArchive } from './chatHistory';

function thread(id: string, project_id?: string, persona = 'cto', deleted_at?: number): HypervisorThread {
  return { id, title: id, project_id, persona, deleted_at, status: 'idle', assistant: 'claude', created_at: 1, updated_at: 1 };
}

describe('CTO history scope', () => {
  const list = [thread('a', 'A'), thread('b', 'B'), thread('root'), thread('plain', 'A', ''), thread('gone', 'A', 'cto', 2)];
  it('keeps only CTO chats for the selected project, including its trash', () => {
    expect(ctoThreads(list, 'A').map(t => t.id)).toEqual(['a', 'gone']);
    expect(ctoThreads(list, 'B').map(t => t.id)).toEqual(['b']);
  });
  it('Workspace contains only unbound CTO chats', () => {
    expect(ctoThreads(list, null).map(t => t.id)).toEqual(['root']);
  });
  it('handles an empty list', () => expect(ctoThreads([], 'A')).toEqual([]));
});

describe('selection after archive', () => {
  it('opens the first remaining chat when the active chat was archived', () => {
    expect(selectionAfterArchive('a', 'a', [thread('b'), thread('c')])).toBe('b');
  });
  it('opens a fresh composer when the last chat was archived', () => {
    expect(selectionAfterArchive('a', 'a', [])).toBeNull();
  });
  it('preserves a different selection, including a new composer', () => {
    expect(selectionAfterArchive('b', 'a', [thread('b')])).toBe('b');
    expect(selectionAfterArchive(null, 'a', [thread('b')])).toBeNull();
  });
  it('does not reopen the archived ID or another deleted thread from a stale list', () => {
    expect(selectionAfterArchive('a', 'a', [thread('a'), thread('gone', undefined, 'cto', 2), thread('b')])).toBe('b');
  });
  it('only chooses from the current project', () => {
    const list = [thread('b', 'B'), thread('a2', 'A')];
    expect(selectionAfterArchive('a1', 'a1', ctoThreads(list, 'A'))).toBe('a2');
  });
});
