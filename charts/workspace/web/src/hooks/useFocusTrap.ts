import { useEffect } from 'preact/hooks';
import type { RefObject } from 'preact';

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusable(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => !el.hasAttribute('hidden') && el.getAttribute('aria-hidden') !== 'true',
  );
}

/**
 * Ref-counted `inert`/`aria-hidden` on the app's background content while any
 * modal is open. Mirrors useScrollLock's counter so nested overlays
 * (Drawer-in-Sheet, Onboarding-over-Palette) only restore the background once
 * the last layer closes. Targets `.app-content` — the wrapper around the
 * Shell's chrome — so the open dialog (a sibling) stays interactive.
 */
let inertCount = 0;
function acquireInert(): () => void {
  const bg = typeof document !== 'undefined' ? document.querySelector('.app-content') : null;
  if (!bg) return () => {};
  if (inertCount === 0) {
    bg.setAttribute('inert', '');
    bg.setAttribute('aria-hidden', 'true');
  }
  inertCount += 1;
  return () => {
    inertCount -= 1;
    if (inertCount === 0) {
      bg.removeAttribute('inert');
      bg.removeAttribute('aria-hidden');
    }
  };
}

/**
 * Trap keyboard focus within `ref` while `active`, restore focus to whatever
 * was focused when the trap engaged (the trigger) on close, and mark the
 * background inert. Composes with `useEscape`/`useScrollLock` — the three
 * together are what makes a `role="dialog"` actually behave like a modal for
 * keyboard and assistive-tech users.
 */
export function useFocusTrap(active: boolean, ref: RefObject<HTMLElement>): void {
  useEffect(() => {
    if (!active) return;
    const container = ref.current;
    if (!container) return;

    // Remember the trigger so we can hand focus back when the modal closes.
    const trigger = document.activeElement as HTMLElement | null;
    const releaseInert = acquireInert();

    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Tab') return;
      const items = focusable(container as HTMLElement);
      if (items.length === 0) {
        // Nothing focusable inside — keep focus pinned to the dialog itself.
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement;
      if (e.shiftKey) {
        if (current === first || !(container as HTMLElement).contains(current)) {
          e.preventDefault();
          last.focus();
        }
      } else if (current === last || !(container as HTMLElement).contains(current)) {
        e.preventDefault();
        first.focus();
      }
    }

    container.addEventListener('keydown', onKeyDown);
    return () => {
      container.removeEventListener('keydown', onKeyDown);
      releaseInert();
      // Hand focus back to the trigger so keyboard users land where they left
      // off. By cleanup time the dialog is being torn down (focus has reverted
      // to <body>), so restore unconditionally — but only if the trigger is
      // still in the document.
      const stillConnected = trigger && (trigger.isConnected ?? document.contains(trigger));
      if (stillConnected && typeof trigger.focus === 'function') {
        trigger.focus();
      }
    };
  }, [active, ref]);
}

/** Test-only: reset the shared inert counter between tests. */
export function _resetInertCount(): void {
  inertCount = 0;
}
