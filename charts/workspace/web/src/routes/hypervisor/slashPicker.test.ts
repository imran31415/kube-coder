import { describe, expect, it } from 'vitest';
import { supportsSlash, slashToken, matchCommands } from './slashPicker';
import type { HypervisorCommand } from '../../api/hypervisor';

function cmd(name: string, kind: 'command' | 'skill' = 'skill'): HypervisorCommand {
  return { name, kind, description: '', argument_hint: '', scope: 'project' };
}

describe('supportsSlash', () => {
  it('is true only for claude (the confirmed-resolving adapter)', () => {
    expect(supportsSlash('claude')).toBe(true);
    expect(supportsSlash('ante')).toBe(false);
    expect(supportsSlash('codex')).toBe(false);
    expect(supportsSlash('')).toBe(false);
    expect(supportsSlash(null)).toBe(false);
    expect(supportsSlash(undefined)).toBe(false);
  });
});

describe('slashToken', () => {
  it('extracts the token while typing a bare leading-slash word', () => {
    expect(slashToken('/')).toBe('');
    expect(slashToken('/kc')).toBe('kc');
    expect(slashToken('/kc-issue')).toBe('kc-issue');
    expect(slashToken('/git:commit')).toBe('git:commit');
  });

  it('lowercases the token', () => {
    expect(slashToken('/KC-Issue')).toBe('kc-issue');
  });

  it('is null once the command is complete or not a command at all', () => {
    expect(slashToken('/kc-issue 302')).toBeNull(); // space → complete
    expect(slashToken('/kc\n')).toBeNull(); // newline → complete
    expect(slashToken('hello')).toBeNull(); // no leading slash
    expect(slashToken(' /kc')).toBeNull(); // leading whitespace
    expect(slashToken('')).toBeNull();
  });
});

describe('matchCommands', () => {
  const all = [cmd('deploy'), cmd('kc-issue'), cmd('kc-ship'), cmd('review')];

  it('returns everything (capped) for an empty query', () => {
    expect(matchCommands(all, '').map((c) => c.name)).toEqual([
      'deploy',
      'kc-issue',
      'kc-ship',
      'review',
    ]);
  });

  it('ranks prefix matches before substring matches', () => {
    const items = [cmd('review'), cmd('kc-issue'), cmd('kc-ship')];
    // "sh" is a substring of kc-ship but prefix of nothing here.
    expect(matchCommands(items, 'sh').map((c) => c.name)).toEqual(['kc-ship']);
    // "kc" is a prefix of both kc-* entries.
    expect(matchCommands(items, 'kc').map((c) => c.name)).toEqual(['kc-issue', 'kc-ship']);
  });

  it('is case-insensitive', () => {
    expect(matchCommands(all, 'KC').map((c) => c.name)).toEqual(['kc-issue', 'kc-ship']);
  });

  it('caps results at 8', () => {
    const many = Array.from({ length: 20 }, (_, i) => cmd(`cmd-${i}`));
    expect(matchCommands(many, '').length).toBe(8);
    expect(matchCommands(many, 'cmd').length).toBe(8);
  });

  it('returns [] when nothing matches', () => {
    expect(matchCommands(all, 'zzz')).toEqual([]);
  });
});
