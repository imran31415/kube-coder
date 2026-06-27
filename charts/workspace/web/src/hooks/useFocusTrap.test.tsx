import { render } from '@testing-library/preact';
import { useRef } from 'preact/hooks';
import { describe, expect, it, beforeEach } from 'vitest';
import { useFocusTrap, _resetInertCount } from './useFocusTrap';

function Trapped({ active }: { active: boolean }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useFocusTrap(active, ref);
  return (
    <div>
      <button id="outside" type="button">outside</button>
      {active && (
        <div ref={ref} data-testid="dialog">
          <button id="first" type="button">first</button>
          <button id="mid" type="button">mid</button>
          <button id="last" type="button">last</button>
        </div>
      )}
    </div>
  );
}

function tab(shift = false) {
  document.activeElement?.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Tab', shiftKey: shift, bubbles: true }),
  );
}

beforeEach(() => {
  _resetInertCount();
  document.body.innerHTML = '';
});

describe('useFocusTrap', () => {
  it('wraps Tab from the last focusable back to the first', () => {
    render(<Trapped active />);
    const last = document.getElementById('last') as HTMLButtonElement;
    last.focus();
    expect(document.activeElement).toBe(last);
    tab();
    expect(document.activeElement).toBe(document.getElementById('first'));
  });

  it('wraps Shift+Tab from the first focusable to the last', () => {
    render(<Trapped active />);
    const first = document.getElementById('first') as HTMLButtonElement;
    first.focus();
    tab(true);
    expect(document.activeElement).toBe(document.getElementById('last'));
  });

  it('restores focus to the trigger when the trap deactivates', () => {
    const { rerender } = render(<Trapped active={false} />);
    const outside = document.getElementById('outside') as HTMLButtonElement;
    outside.focus();
    expect(document.activeElement).toBe(outside);

    rerender(<Trapped active />);
    // Move focus into the dialog as a real open would.
    (document.getElementById('first') as HTMLButtonElement).focus();

    rerender(<Trapped active={false} />);
    expect(document.activeElement).toBe(outside);
  });

  it('marks the background .app-content inert while active and clears it after', () => {
    const bg = document.createElement('div');
    bg.className = 'app-content';
    document.body.appendChild(bg);

    const { rerender } = render(<Trapped active />);
    expect(bg.hasAttribute('inert')).toBe(true);
    expect(bg.getAttribute('aria-hidden')).toBe('true');

    rerender(<Trapped active={false} />);
    expect(bg.hasAttribute('inert')).toBe(false);
    expect(bg.hasAttribute('aria-hidden')).toBe(false);
  });
});
