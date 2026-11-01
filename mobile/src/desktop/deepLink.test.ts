/**
 * `kubecoder://` links and desktop notification clicks → the push tap router
 * (#833). Both must resolve to exactly the target a phone push with the same
 * ref would open, and nothing a link says can make the app open another URL.
 */
import { describe, expect, it } from 'vitest';
import { pushTargetFromData } from '../util/push';
import { pushDataFromTarget, refFromDeepLink } from './deepLink';
import { embedNext } from './embedPath';

const target = (url: string) => pushTargetFromData(pushDataFromTarget({ url }));

describe('refFromDeepLink', () => {
  it('reads the path spelling', () => {
    expect(refFromDeepLink('kubecoder://board/b-1/item-9')).toBe('board:b-1:item-9');
    expect(refFromDeepLink('kubecoder://task/t-123')).toBe('task:t-123');
    expect(refFromDeepLink('kubecoder://thread/th_1/')).toBe('thread:th_1');
    expect(refFromDeepLink('kubecoder://scan/s1')).toBe('scan:s1');
  });

  it('reads the push-ref spelling verbatim, colons in ids included', () => {
    expect(refFromDeepLink('kubecoder://board:b-1:gid://github/Issue/42')).toBe(
      'board:b-1:gid://github/Issue/42',
    );
    expect(refFromDeepLink('kubecoder:task:t-1')).toBe('task:t-1');
  });

  it('decodes escaped ids', () => {
    expect(refFromDeepLink('kubecoder://board/b%201/gid%3A%2F%2Fx')).toBe('board:b 1:gid://x');
  });

  it('is case-insensitive about the scheme and kind', () => {
    expect(refFromDeepLink('KubeCoder://TASK/t-1')).toBe('task:t-1');
  });

  it('falls back to the Feed for anything else', () => {
    for (const u of [
      '',
      'kubecoder://',
      'kubecoder://feed',
      'kubecoder://board/only-one',
      'kubecoder://task/a/b',
      'kubecoder://unknown/x',
      'kubecoder://evil:https://evil.com',
      'kubecoder://task/%E0%A4%A',
      'https://evil.com/task/1',
      `kubecoder://task/${'x'.repeat(3000)}`,
    ]) {
      expect(refFromDeepLink(u)).toBe('');
    }
  });
});

describe('deep link → target', () => {
  it('lands where the same push would', () => {
    expect(target('kubecoder://board/b1/i1')).toEqual({ kind: 'board', boardId: 'b1', itemId: 'i1' });
    expect(target('kubecoder://task/t1')).toEqual({ kind: 'task', id: 't1' });
    expect(target('kubecoder://thread/th1')).toEqual({ kind: 'thread', id: 'th1' });
    expect(target('kubecoder://scan/s1')).toEqual({ kind: 'scan', id: 's1' });
    expect(target('kubecoder://whatever')).toEqual({ kind: 'none' });
  });

  it('can never become an external URL', () => {
    for (const u of ['kubecoder://https://evil.com', 'kubecoder://href:https://evil.com', 'kubecoder:javascript:alert(1)']) {
      expect(target(u).kind).not.toBe('external');
    }
  });
});

describe('notification click → target', () => {
  it('carries the alert ref and its Feed row, so the tap also marks it read', () => {
    expect(pushDataFromTarget({ ref: 'board:b1:i1', feedId: 'f9' })).toEqual({ ref: 'board:b1:i1', feedId: 'f9' });
    expect(pushTargetFromData(pushDataFromTarget({ ref: 'task:t1', feedId: 'f1' }))).toEqual({ kind: 'task', id: 't1' });
  });

  it('opens the Feed when the alert had no ref', () => {
    expect(pushTargetFromData(pushDataFromTarget({ feedId: 'f1' }))).toEqual({ kind: 'none' });
  });
});

describe('embedNext', () => {
  it('pulls the app path out of the session bootstrap URL', () => {
    expect(embedNext('https://me.example.com/api/claude/apps/session?next=%2Fapi%2Fapp-proxy%2F3000%2F')).toBe(
      '/api/app-proxy/3000/',
    );
    expect(
      embedNext('https://me.example.com/api/claude/apps/session?next=%2Fapi%2Fterminal-proxy%2F%3Ft%3D1'),
    ).toBe('/api/terminal-proxy/?t=1');
  });

  it('is null for anything that is not a bootstrap', () => {
    expect(embedNext('https://me.example.com/api/files/view?path=a.pdf')).toBeNull();
    expect(embedNext('https://me.example.com/api/app-proxy/3000/')).toBeNull();
  });
});
