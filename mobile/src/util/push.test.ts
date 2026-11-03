import { describe, it, expect } from 'vitest';
import { pushFromOtherWorkspace, pushTapRoute, pushTargetFromData, tapRoute } from './push';

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

describe('pushTargetFromData · board review pushes (#692)', () => {
  /* The server already pushes these: push_notify._build_messages copies the
     feed link's ref into data.ref, and BoardReview emits
     "board:<board_id>:<item_id>". Mobile was the only side that could not read
     it, so every board review push landed on the Feed. */
  it('maps a board ref to the board target', () => {
    expect(pushTargetFromData({ ref: 'board:acme:412', waiting: true })).toEqual({
      kind: 'board', boardId: 'acme', itemId: '412',
    });
  });

  it('keeps a colon-bearing item id whole', () => {
    expect(pushTargetFromData({ ref: 'board:kube-coder-gh:I_kwDOA:4102' })).toEqual({
      kind: 'board', boardId: 'kube-coder-gh', itemId: 'I_kwDOA:4102',
    });
  });

  it('falls back to none for a malformed board ref, so the tap lands on the Feed', () => {
    expect(pushTargetFromData({ ref: 'board:acme' })).toEqual({ kind: 'none' });
    expect(pushTargetFromData({ ref: 'board:' })).toEqual({ kind: 'none' });
  });

  it('trims whitespace around a board ref too', () => {
    expect(pushTargetFromData({ ref: '  board:acme:412 ' })).toEqual({
      kind: 'board', boardId: 'acme', itemId: '412',
    });
  });
});

describe('pushTapRoute · Build publishing pushes (#710)', () => {
  const host = 'https://ws.example';
  it('opens the Build on its Changes sheet', () => {
    expect(pushTapRoute({ source: 'build-publish', workspaceHost: host, ref: 'task:t1' }, host)).toEqual({
      kind: 'screen', tab: 'Tasks', screen: 'TaskDetail', params: { id: 't1', tab: 'changes' },
    });
  });

  it('ignores a trailing slash on either host', () => {
    expect(pushFromOtherWorkspace({ source: 'build-publish', workspaceHost: `${host}/` }, host)).toBe(false);
    expect(pushFromOtherWorkspace({ source: 'build-publish', workspaceHost: host }, `${host}//`)).toBe(false);
  });

  it('sends a push from another workspace, or with no sender, to the Feed', () => {
    expect(pushTapRoute({ source: 'build-publish', workspaceHost: 'https://old.example', ref: 'task:t1' }, host))
      .toEqual({ kind: 'screen', tab: 'Feed' });
    expect(pushTapRoute({ source: 'build-publish', ref: 'task:t1' }, host)).toEqual({ kind: 'screen', tab: 'Feed' });
  });

  it('leaves every other push exactly as tapRoute routes it', () => {
    expect(pushFromOtherWorkspace({ ref: 'task:t1' }, host)).toBe(false);
    expect(pushTapRoute({ ref: 'task:t1' }, host)).toEqual(tapRoute(pushTargetFromData({ ref: 'task:t1' })));
    expect(pushTapRoute({ ref: 'thread:th', workspaceHost: 'https://other.example' }, host))
      .toEqual({ kind: 'screen', tab: 'Hypervisor', params: { openThreadId: 'th' } });
  });
});
