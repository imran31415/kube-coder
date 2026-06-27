import type { ComponentChildren } from 'preact';
import { useRef } from 'preact/hooks';
import { useEscape } from '../hooks/useEscape';
import { useScrollLock } from '../hooks/useScrollLock';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { Portal } from './Portal';
import './Modal.css';

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  /** Accessible name as a plain string. Prefer `labelledBy` when a visible
   *  heading exists inside `children`. */
  label?: string;
  /** id of a heading rendered inside `children` that names the dialog. */
  labelledBy?: string;
  /** `alertdialog` for destructive confirmations, `dialog` otherwise. */
  role?: 'dialog' | 'alertdialog';
  /** Max dialog width in px. Default 420. */
  width?: number;
  /** Extra class on the dialog box. */
  class?: string;
  children: ComponentChildren;
}

/**
 * Shared centered modal primitive: a scrim plus a focus-trapped dialog box.
 * Bundles the three behaviours every modal needs — Esc to close
 * (`useEscape`), background scroll lock (`useScrollLock`), and focus
 * trap + restore + background `inert` (`useFocusTrap`) — so call sites only
 * supply content. Clicking the scrim closes; clicks inside the box don't.
 */
export function Modal({
  open,
  onClose,
  label,
  labelledBy,
  role = 'dialog',
  width = 420,
  class: className,
  children,
}: ModalProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEscape(open, onClose);
  useScrollLock(open);
  useFocusTrap(open, ref);
  if (!open) return null;
  return (
    <Portal>
      <div class="modal-scrim" onClick={onClose} role="presentation">
        <div
          ref={ref}
          class={`modal-dialog ${className ?? ''}`.trim()}
          style={{ maxWidth: `${width}px` }}
          role={role}
          aria-modal="true"
          aria-label={label}
          aria-labelledby={labelledBy}
          onClick={(e) => e.stopPropagation()}
        >
          {children}
        </div>
      </div>
    </Portal>
  );
}
