import { render } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { usePoll } from './usePoll';

function Polling({ fn, interval, enabled = true, pauseOnHidden = true }: { fn: () => void | Promise<void>; interval: number; enabled?: boolean; pauseOnHidden?: boolean }) {
  usePoll(fn, interval, { enabled, pauseOnHidden });
  return <div>poll</div>;
}

describe('usePoll', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('does nothing when enabled=false', async () => {
    const fn = vi.fn();
    render(<Polling fn={fn} interval={100} enabled={false} />);
    await vi.advanceTimersByTimeAsync(500);
    expect(fn).not.toHaveBeenCalled();
  });

  // advanceTimersByTimeAsync(0) flushes pending microtasks without firing
  // any setTimeout — useful to let the initial `void tick()` resolve.
  const flushMicro = () => vi.advanceTimersByTimeAsync(0);

  it('calls fn immediately and then every intervalMs', async () => {
    const fn = vi.fn();
    render(<Polling fn={fn} interval={100} />);
    await flushMicro();
    expect(fn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(110);
    expect(fn).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(100);
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it('does not double-fire when previous run is still pending', async () => {
    let resolveLater: (() => void) = () => {};
    const fn = vi.fn(() => new Promise<void>((r) => { resolveLater = r; }));
    render(<Polling fn={fn} interval={50} />);
    await flushMicro();
    expect(fn).toHaveBeenCalledTimes(1);
    // advance past several intervals while the promise is still pending
    await vi.advanceTimersByTimeAsync(300);
    // still only 1 call — the in-flight guard kept us from stacking
    expect(fn).toHaveBeenCalledTimes(1);
    resolveLater();
  });

  it('stops polling when the component unmounts', async () => {
    const fn = vi.fn();
    const { unmount } = render(<Polling fn={fn} interval={50} />);
    await flushMicro();
    expect(fn).toHaveBeenCalledTimes(1);
    unmount();
    await vi.advanceTimersByTimeAsync(500);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('skips ticks when document.visibilityState is hidden', async () => {
    const fn = vi.fn();
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    render(<Polling fn={fn} interval={50} />);
    await flushMicro();
    await vi.advanceTimersByTimeAsync(300);
    expect(fn).not.toHaveBeenCalled();
    // restore for other tests
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  it('backs off on consecutive errors', async () => {
    const fn = vi.fn(() => Promise.reject(new Error('boom')));
    render(<Polling fn={fn} interval={100} />);
    await flushMicro();
    expect(fn).toHaveBeenCalledTimes(1);
    // base interval 100, backoff 1.5 ⇒ next tick at ~150ms.
    await vi.advanceTimersByTimeAsync(110);
    expect(fn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(60);
    expect(fn).toHaveBeenCalledTimes(2);
  });
});
