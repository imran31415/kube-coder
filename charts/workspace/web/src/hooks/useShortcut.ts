import { useEffect } from 'preact/hooks';

export interface ShortcutSpec {
  /** Lowercased key, e.g. 'k', 'escape', '/', '?' */
  key: string;
  /** Cmd on macOS, Ctrl elsewhere. */
  meta?: boolean;
  shift?: boolean;
  /** Skip when the user is typing in an input/textarea/contenteditable. */
  allowInInput?: boolean;
}

function isEditable(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (el.isContentEditable) return true;
  return false;
}

export function useShortcut(spec: ShortcutSpec, handler: (e: KeyboardEvent) => void): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== spec.key.toLowerCase()) return;
      if (spec.meta && !(e.metaKey || e.ctrlKey)) return;
      if (!spec.meta && (e.metaKey || e.ctrlKey)) return;
      if (spec.shift && !e.shiftKey) return;
      if (!spec.allowInInput && isEditable(e.target)) return;
      handler(e);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [spec.key, spec.meta, spec.shift, spec.allowInInput, handler]);
}
