import { useEffect, useRef, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import './PopoverMenu.css';

/**
 * Tiny anchored popover for overflow / settings menus. Anchors to a
 * trigger element (rendered via `trigger` render-prop), opens on click,
 * closes on Escape / outside click. Single-purpose — drop into any
 * topbar / toolbar where you'd otherwise render 4-6 buttons.
 *
 * Positioning is dead simple: anchor.getBoundingClientRect() →
 * absolute-positioned panel below the anchor, right-aligned to the
 * anchor's right edge so it doesn't overflow the viewport on narrow
 * screens.
 */
export function PopoverMenu({
  trigger,
  children,
  align = 'right',
  width = 240,
}: {
  trigger: (props: {
    onClick: (e: MouseEvent) => void;
    'aria-expanded': boolean;
    ref: (el: HTMLElement | null) => void;
  }) => ComponentChildren;
  children: ComponentChildren | ((close: () => void) => ComponentChildren);
  align?: 'left' | 'right';
  width?: number;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  function close() { setOpen(false); }

  // Recompute panel position whenever it opens. Anchored just below the
  // trigger, aligned by `align` to the trigger's left/right edge.
  useEffect(() => {
    if (!open) { setPos(null); return; }
    const a = triggerRef.current;
    if (!a) return;
    const r = a.getBoundingClientRect();
    const top = r.bottom + 6;
    const left = align === 'right'
      ? Math.max(8, r.right - width)
      : Math.min(window.innerWidth - width - 8, r.left);
    setPos({ top, left });
  }, [open, align, width]);

  // Outside click + Escape to dismiss.
  useEffect(() => {
    if (!open) return;
    function onMouseDown(e: MouseEvent) {
      const t = e.target as Node | null;
      if (panelRef.current?.contains(t)) return;
      if (triggerRef.current?.contains(t)) return;
      close();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') close();
    }
    window.addEventListener('mousedown', onMouseDown);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onMouseDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <>
      {trigger({
        onClick: (e) => { e.stopPropagation(); setOpen((o) => !o); },
        'aria-expanded': open,
        ref: (el) => { triggerRef.current = el; },
      })}
      {open && pos && (
        <div
          ref={panelRef}
          class="popover-menu"
          role="menu"
          style={{ top: `${pos.top}px`, left: `${pos.left}px`, width: `${width}px` }}
        >
          {typeof children === 'function' ? children(close) : children}
        </div>
      )}
    </>
  );
}

/** Single row inside a PopoverMenu. Use `danger` for destructive ops. */
export function PopoverItem({
  onClick,
  disabled,
  danger,
  hint,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
  hint?: string;
  children: ComponentChildren;
}) {
  return (
    <button
      type="button"
      class={`popover-item ${danger ? 'popover-item-danger' : ''}`}
      onClick={onClick}
      disabled={disabled}
      role="menuitem"
    >
      <span class="popover-item-label">{children}</span>
      {hint && <span class="popover-item-hint muted mono">{hint}</span>}
    </button>
  );
}

/** Visual section header inside a PopoverMenu. */
export function PopoverSection({ children }: { children: ComponentChildren }) {
  return <div class="popover-section">{children}</div>;
}

export function PopoverDivider() {
  return <div class="popover-divider" aria-hidden="true" />;
}
