import { describe, it, expect } from 'vitest';
import { LAST_BOARD_KEY, pickBoard, rememberBoard } from './lastBoard';

/**
 * Which board the phone opens on (#712).
 *
 * The bug was not that the wrong board was remembered — nothing was. The
 * screen took `list[0]`, so a workspace with two trackers made the queue you
 * were actually working one tap away every time you opened the app.
 */

function mkStorage(seed: Record<string, string> = {}) {
  const map = new Map(Object.entries(seed));
  return {
    map,
    getItem: async (k: string) => map.get(k) ?? null,
    setItem: async (k: string, v: string) => {
      map.set(k, v);
    },
  };
}

describe('lastBoard', () => {
  it('opens the remembered board', async () => {
    const storage = mkStorage({ [LAST_BOARD_KEY]: 'kube-coder-gh' });
    expect(
      await pickBoard(storage, [{ id: 'acme-jira' }, { id: 'kube-coder-gh' }]),
    ).toBe('kube-coder-gh');
  });

  it('falls back to the first board when nothing is remembered', async () => {
    const storage = mkStorage();
    expect(
      await pickBoard(storage, [{ id: 'acme-jira' }, { id: 'kube-coder-gh' }]),
    ).toBe('acme-jira');
  });

  it('falls back when the remembered board was disconnected', async () => {
    const storage = mkStorage({ [LAST_BOARD_KEY]: 'gone' });
    expect(await pickBoard(storage, [{ id: 'acme-jira' }])).toBe('acme-jira');
  });

  it('returns null when there are no boards at all', async () => {
    expect(await pickBoard(mkStorage(), [])).toBeNull();
  });

  it('remembers a choice, and ignores a blank one', async () => {
    const storage = mkStorage();
    await rememberBoard(storage, 'kube-coder-gh');
    expect(storage.map.get(LAST_BOARD_KEY)).toBe('kube-coder-gh');
    await rememberBoard(storage, '');
    expect(storage.map.get(LAST_BOARD_KEY)).toBe('kube-coder-gh');
  });

  it('survives storage that throws — a preference must not break the screen', async () => {
    const broken = {
      getItem: async () => {
        throw new Error('no storage');
      },
      setItem: async () => {
        throw new Error('no storage');
      },
    };
    await expect(rememberBoard(broken, 'acme-jira')).resolves.toBeUndefined();
    expect(await pickBoard(broken, [{ id: 'acme-jira' }])).toBe('acme-jira');
  });
});
