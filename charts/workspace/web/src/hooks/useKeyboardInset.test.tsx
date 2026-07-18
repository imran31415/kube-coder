import { render, cleanup } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useKeyboardInset } from './useKeyboardInset';

function Probe() {
  useKeyboardInset();
  return <div>probe</div>;
}

// Minimal visualViewport stub whose listeners we can fire on demand.
function stubVisualViewport(height: number, offsetTop = 0) {
  const listeners: Record<string, Array<() => void>> = { resize: [], scroll: [] };
  const vv = {
    height,
    offsetTop,
    addEventListener: (t: string, cb: () => void) => listeners[t]?.push(cb),
    removeEventListener: (t: string, cb: () => void) => {
      listeners[t] = (listeners[t] ?? []).filter((f) => f !== cb);
    },
  };
  Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true, writable: true });
  return {
    fire(next: Partial<{ height: number; offsetTop: number }> = {}) {
      Object.assign(vv, next);
      [...listeners.resize, ...listeners.scroll].forEach((f) => f());
    },
  };
}

describe('useKeyboardInset', () => {
  const root = document.documentElement;

  beforeEach(() => {
    Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true, writable: true });
    delete root.dataset.keyboardOpen;
  });
  afterEach(() => {
    cleanup();
    delete root.dataset.keyboardOpen;
    vi.restoreAllMocks();
  });

  it('marks keyboard closed when the visual viewport fills the window', () => {
    stubVisualViewport(800);
    render(<Probe />);
    expect(root.dataset.keyboardOpen).toBe('false');
  });

  it('marks keyboard open when a large chunk is hidden below the viewport', () => {
    const vp = stubVisualViewport(800);
    render(<Probe />);
    vp.fire({ height: 460 }); // ~340px keyboard
    expect(root.dataset.keyboardOpen).toBe('true');
    vp.fire({ height: 800 }); // keyboard dismissed
    expect(root.dataset.keyboardOpen).toBe('false');
  });

  it('ignores small overlaps like URL-bar collapse', () => {
    const vp = stubVisualViewport(800);
    render(<Probe />);
    vp.fire({ height: 720 }); // 80px — below the 120px threshold
    expect(root.dataset.keyboardOpen).toBe('false');
  });

  it('cleans up the attribute on unmount', () => {
    const vp = stubVisualViewport(800);
    const { unmount } = render(<Probe />);
    vp.fire({ height: 460 });
    expect(root.dataset.keyboardOpen).toBe('true');
    unmount();
    expect(root.dataset.keyboardOpen).toBeUndefined();
  });
});
