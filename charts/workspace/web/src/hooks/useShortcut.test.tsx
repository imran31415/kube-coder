import { render } from '@testing-library/preact';
import { describe, expect, it, vi } from 'vitest';
import { useShortcut } from './useShortcut';

function Probe({ onFire }: { onFire: () => void }) {
  useShortcut({ key: 'k', meta: true, allowInInput: true }, onFire);
  return <input data-testid="probe-input" />;
}

function press(opts: KeyboardEventInit, target: EventTarget = window) {
  // happy-dom doesn't always route fireEvent.keyDown(window, …) to window
  // listeners — dispatch via the native API which definitely does.
  target.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, ...opts }));
}

describe('useShortcut', () => {
  it('fires on Cmd+K / Ctrl+K', () => {
    const fire = vi.fn();
    render(<Probe onFire={fire} />);
    press({ key: 'k', ctrlKey: true });
    expect(fire).toHaveBeenCalledTimes(1);
    press({ key: 'k', metaKey: true });
    expect(fire).toHaveBeenCalledTimes(2);
  });

  it('does not fire on plain k', () => {
    const fire = vi.fn();
    render(<Probe onFire={fire} />);
    press({ key: 'k' });
    expect(fire).not.toHaveBeenCalled();
  });

  it('skips while typing in an input unless allowInInput', () => {
    const fire = vi.fn();
    function P({ onFire }: { onFire: () => void }) {
      useShortcut({ key: '/', meta: false }, onFire);
      return <input data-testid="input" />;
    }
    const { getByTestId } = render(<P onFire={fire} />);
    const input = getByTestId('input') as HTMLInputElement;
    // Dispatch with the input as the target so isEditable() kicks in.
    input.dispatchEvent(new KeyboardEvent('keydown', { key: '/', bubbles: true }));
    expect(fire).not.toHaveBeenCalled();
  });
});
