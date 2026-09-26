import { describe, expect, it } from 'vitest';
import { formatDiffStat, isGitWorkdir, removeErrorKind, worktreeRemoved } from './worktree';

const DIRS = [
  { path: '/home/dev/app', is_git_repo: true },
  { path: '/home/dev/notes', is_git_repo: false },
  { path: '/home/dev/app-two/', is_git_repo: true },
];

describe('formatDiffStat', () => {
  it('matches the web wording', () => {
    expect(formatDiffStat(null)).toBe('');
    expect(formatDiffStat({ files_changed: 0, insertions: 0, deletions: 0 })).toBe('No changes');
    expect(formatDiffStat({ files_changed: 1, insertions: 0, deletions: 0 })).toBe('1 file changed');
    expect(formatDiffStat({ files_changed: 3, insertions: 40, deletions: 12 })).toBe('3 files changed (+40 −12)');
  });
});

describe('isGitWorkdir', () => {
  it('accepts a git folder and folders inside it', () => {
    expect(isGitWorkdir('/home/dev/app', DIRS)).toBe(true);
    expect(isGitWorkdir('/home/dev/app/', DIRS)).toBe(true);
    expect(isGitWorkdir('/home/dev/app/web', DIRS)).toBe(true);
    expect(isGitWorkdir(' /home/dev/app-two ', DIRS)).toBe(true);
  });

  it('refuses non-git folders, lookalike siblings and unknown paths', () => {
    expect(isGitWorkdir('/home/dev/notes', DIRS)).toBe(false);
    expect(isGitWorkdir('/home/dev/application', DIRS)).toBe(false);   // not inside /home/dev/app
    expect(isGitWorkdir('/home/dev', DIRS)).toBe(false);
    expect(isGitWorkdir('', DIRS)).toBe(false);
    expect(isGitWorkdir('/home/dev/app', [])).toBe(false);
  });
});

// api/client.ts pulls in React Native, which this node runner cannot load;
// the helper only reads `.code`, which is what ApiError carries.
const apiError = (message: string, code?: string) => Object.assign(new Error(message), { code });

describe('removeErrorKind', () => {
  it('reads the server code, not the message', () => {
    expect(removeErrorKind(apiError('has 2 uncommitted', 'dirty'))).toBe('dirty');
    expect(removeErrorKind(apiError('Build is running', 'live'))).toBe('live');
    expect(removeErrorKind(apiError('dirty dirty dirty'))).toBe('other');
    expect(removeErrorKind(null)).toBe('other');
  });
});

describe('worktreeRemoved', () => {
  it('understands both the list brief and the raw detail record', () => {
    expect(worktreeRemoved({ removed: true })).toBe(true);
    expect(worktreeRemoved({ removed_at: 1789000000 })).toBe(true);
    expect(worktreeRemoved({ removed: false, removed_at: null })).toBe(false);
    expect(worktreeRemoved(undefined)).toBe(false);
  });
});
