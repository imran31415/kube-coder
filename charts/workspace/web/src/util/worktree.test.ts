import { describe, expect, it } from 'vitest';
import { diffStatLabel, sha7, shouldWarnSharedTree, worktreeStateOf } from './worktree';

describe('diffStatLabel', () => {
  it('words the counts the same way everywhere', () => {
    expect(diffStatLabel(null)).toBe('');
    expect(diffStatLabel({ files_changed: 0, insertions: 0, deletions: 0 })).toBe('No changes');
    expect(diffStatLabel({ files_changed: 1, insertions: 0, deletions: 0 })).toBe('1 file changed');
    expect(diffStatLabel({ files_changed: 3, insertions: 40, deletions: 12 }))
      .toBe('3 files changed (+40 −12)');
  });
});

describe('shouldWarnSharedTree', () => {
  it('warns only when several agents would share one checkout', () => {
    expect(shouldWarnSharedTree('/home/dev/app', false, 3)).toBe(true);
    expect(shouldWarnSharedTree('/home/dev/app', true, 3)).toBe(false);   // isolated
    expect(shouldWarnSharedTree('/home/dev/app', false, 1)).toBe(false);  // one at a time
    expect(shouldWarnSharedTree('', false, 5)).toBe(false);               // tracker-only
  });
});

describe('worktreeStateOf', () => {
  it('names the owner state', () => {
    expect(worktreeStateOf({ task_id: '', live: false, owner_status: '' })).toBe('manual');
    expect(worktreeStateOf({ task_id: 't', live: true, owner_status: 'running' })).toBe('live');
    expect(worktreeStateOf({ task_id: 't', live: false, owner_status: 'completed' })).toBe('finished');
    expect(worktreeStateOf({ task_id: 't', live: false, owner_status: '' })).toBe('orphaned');
  });
});

describe('sha7', () => {
  it('shortens', () => {
    expect(sha7('0123456789abcdef')).toBe('0123456');
    expect(sha7('')).toBe('');
    expect(sha7(null)).toBe('');
  });
});
