/** Spinners stop while the window is hidden (#833). */
import { describe, expect, it } from 'vitest';
import { HIDDEN_ATTR, installAnimationPause, PAUSE_CSS, type DocLike } from './animationPause';

function fakeDoc() {
  const attrs = new Map<string, string>();
  const appended: { textContent: string | null }[] = [];
  const doc: DocLike = {
    documentElement: {
      setAttribute: (n, v) => void attrs.set(n, v),
      removeAttribute: (n) => void attrs.delete(n),
    },
    head: { appendChild: (node) => appended.push(node as { textContent: string | null }) },
    createElement: () => ({ textContent: null }),
  };
  return { doc, attrs, appended };
}

function visibility(initial: boolean) {
  const ls = new Set<(v: boolean) => void>();
  return {
    initial,
    subscribe: (l: (v: boolean) => void) => {
      ls.add(l);
      return () => void ls.delete(l);
    },
    set: (v: boolean) => ls.forEach((l) => l(v)),
    size: () => ls.size,
  };
}

describe('installAnimationPause', () => {
  it('adds one rule that pauses every animation under a hidden root', () => {
    const { doc, appended } = fakeDoc();
    installAnimationPause(doc, true, visibility(true).subscribe);
    expect(appended).toHaveLength(1);
    expect(appended[0].textContent).toBe(PAUSE_CSS);
    expect(PAUSE_CSS).toContain(`html[${HIDDEN_ATTR}] *`);
    expect(PAUSE_CSS).toContain('animation-play-state: paused');
  });

  it('pauses while hidden and resumes when shown', () => {
    const { doc, attrs } = fakeDoc();
    const v = visibility(true);
    installAnimationPause(doc, true, v.subscribe);
    expect(attrs.has(HIDDEN_ATTR)).toBe(false);
    v.set(false);
    expect(attrs.has(HIDDEN_ATTR)).toBe(true);
    v.set(true);
    expect(attrs.has(HIDDEN_ATTR)).toBe(false);
  });

  it('starts paused when the window is already hidden', () => {
    const { doc, attrs } = fakeDoc();
    installAnimationPause(doc, false, visibility(false).subscribe);
    expect(attrs.has(HIDDEN_ATTR)).toBe(true);
  });

  it('stops following visibility once removed', () => {
    const { doc, attrs } = fakeDoc();
    const v = visibility(true);
    const off = installAnimationPause(doc, true, v.subscribe);
    off();
    expect(v.size()).toBe(0);
    v.set(false);
    expect(attrs.has(HIDDEN_ATTR)).toBe(false);
  });
});
