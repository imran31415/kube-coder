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
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => {
    if (el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true') return false;
    // Skip anything inside an inert subtree within the dialog (e.g. a disabled
    // wizard step). NOTE: this does NOT filter `display:none`/`visibility:hidden`
    // /zero-size elements (no layout in jsdom to measure), and `first`/`last`
    // below use DOM order, not the tabindex-aware tab sequence. Fine for the
    // current dialogs; a future dialog with a CSS-hidden focusable or a positive
    // `tabindex` would need a real visibility/order check here.
    if (el.closest('[inert]')) return false;
    return true;
  });
}

/**
 * Ref-counted `inert`/`aria-hidden` on the app's background content while any
 * modal is open. Mirrors useScrollLock's counter so nested overlays
 * (Drawer-in-Sheet, Onboarding-over-Palette) only restore the background once
 * the last layer closes. Targets `.app-content` — the wrapper around the
 * Shell's chrome — so the open dialog (portaled out to a `<body>` sibling via
 * <Portal>) stays interactive.
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

// Stack of currently-active trap containers. Only the topmost handles Tab so
// nested overlays don't both try to wrap focus into their own container.
const trapStack: HTMLElement[] = [];

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
    trapStack.push(container);

    // Move focus into the dialog on open. Call sites that want a specific
    // element (ConfirmDialog's confirm button, inputs via autoFocus/rAF) run
    // their own focus *after* this effect, so they win; this only guarantees
    // focus lands inside when nothing else claims it. Without it, marking the
    // background inert blurs the trigger to <body>, leaving focus outside the
    // dialog — so the trap below (and screen-reader dialog entry) never engages.
    if (!container.contains(document.activeElement)) {
      const items = focusable(container);
      const target = items[0] ?? container;
      if (target === container && !container.hasAttribute('tabindex')) {
        container.setAttribute('tabindex', '-1');
      }
      target.focus();
    }

    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Tab') return;
      // Only the topmost trap handles Tab; inner dialogs shadow outer ones.
      if (trapStack[trapStack.length - 1] !== container) return;
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

    // Bound to `document`, not `container`: once focus escapes the dialog
    // (e.g. it opened with focus still on <body>) a container-bound listener
    // would never see the Tab to pull it back. The containment checks above
    // handle the recovery.
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      const i = trapStack.indexOf(container);
      if (i !== -1) trapStack.splice(i, 1);
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

/** Test-only: reset the shared trap state between tests. */
export function _resetInertCount(): void {
  inertCount = 0;
  trapStack.length = 0;
}
