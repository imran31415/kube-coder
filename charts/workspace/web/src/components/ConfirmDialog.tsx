import { useEffect, useRef, useState } from 'preact/hooks';
import { useEscape } from '../hooks/useEscape';
import { useScrollLock } from '../hooks/useScrollLock';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { Portal } from './Portal';
import { Button } from './primitives/Button';
import './ConfirmDialog.css';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  body?: string;
  /** Default 'Confirm'. */
  confirmLabel?: string;
  /** Default 'Cancel'. */
  cancelLabel?: string;
  /** Tones the confirm button red for destructive actions. Default false. */
  destructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Modal confirmation dialog. Replaces the native browser `confirm()`
 * which (a) breaks accessibility (b) is blocked in some browsers
 * (c) looks out of place against the rest of the SPA chrome.
 */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  destructive = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  useEscape(open, onCancel);
  useScrollLock(open);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useFocusTrap(open, dialogRef);
  useEffect(() => {
    if (!open) return;
    requestAnimationFrame(() => {
      const btn = dialogRef.current?.querySelector<HTMLButtonElement>('.cd-confirm');
      btn?.focus();
    });
  }, [open]);
  if (!open) return null;
  return (
    <Portal>
      <div class="cd-scrim" onClick={onCancel}>
        <div ref={dialogRef} class="cd-dialog" role="alertdialog" aria-modal="true" aria-labelledby="cd-title" onClick={(e) => e.stopPropagation()}>
          <h2 id="cd-title" class="cd-title">{title}</h2>
          {body && <p class="cd-body">{body}</p>}
          <div class="cd-actions">
            <Button variant="secondary" onClick={onCancel}>{cancelLabel}</Button>
            <Button
              variant={destructive ? 'danger' : 'primary'}
              class="cd-confirm"
              onClick={onConfirm}
            >
              {confirmLabel}
            </Button>
          </div>
        </div>
      </div>
    </Portal>
  );
}

export interface PromptDialogProps {
  open: boolean;
  title: string;
  body?: string;
  initial?: string;
  placeholder?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: (value: string) => void;
  onCancel: () => void;
}

/**
 * Modal prompt — accessible replacement for native `prompt()`. Renders
 * a single text input plus confirm/cancel.
 */
export function PromptDialog({
  open,
  title,
  body,
  initial = '',
  placeholder,
  confirmLabel = 'Save',
  cancelLabel = 'Cancel',
  onConfirm,
  onCancel,
}: PromptDialogProps) {
  const [value, setValue] = useState(initial);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  useEscape(open, onCancel);
  useScrollLock(open);
  useFocusTrap(open, formRef);
  useEffect(() => {
    if (open) {
      setValue(initial);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open, initial]);
  if (!open) return null;
  return (
    <Portal>
      <div class="cd-scrim" onClick={onCancel}>
        <form
          ref={formRef}
          class="cd-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="pd-title"
          onClick={(e) => e.stopPropagation()}
          onSubmit={(e) => { e.preventDefault(); onConfirm(value.trim()); }}
        >
          <h2 id="pd-title" class="cd-title">{title}</h2>
          {body && <p class="cd-body">{body}</p>}
          <input
            ref={inputRef}
            class="cd-input"
            type="text"
            value={value}
            placeholder={placeholder}
            aria-label={title}
            onInput={(e) => setValue((e.target as HTMLInputElement).value)}
          />
          <div class="cd-actions">
            <Button variant="secondary" type="button" onClick={onCancel}>{cancelLabel}</Button>
            <Button variant="primary" type="submit" disabled={!value.trim()}>{confirmLabel}</Button>
          </div>
        </form>
      </div>
    </Portal>
  );
}
