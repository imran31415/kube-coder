import type { ComponentChildren } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import { useEscape } from '../hooks/useEscape';
import { useScrollLock } from '../hooks/useScrollLock';
import { Button } from './primitives/Button';
import { Icon } from './Icon';
import './BottomSheet.css';

export type Snap = 'peek' | 'full';

export interface BottomSheetProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  initialSnap?: Snap;
  children: ComponentChildren;
}

const SNAP_PCT: Record<Snap, number> = {
  peek: 50,
  full: 92,
};

export function BottomSheet({ open, onClose, title, initialSnap = 'peek', children }: BottomSheetProps) {
  useEscape(open, onClose);
  useScrollLock(open);
  const [snap, setSnap] = useState<Snap>(initialSnap);
  const dragStartY = useRef<number | null>(null);
  const dragStartSnap = useRef<Snap>(initialSnap);

  useEffect(() => {
    if (open) setSnap(initialSnap);
  }, [open, initialSnap]);

  function onTouchStart(e: TouchEvent) {
    dragStartY.current = e.touches[0].clientY;
    dragStartSnap.current = snap;
  }
  function onTouchMove(e: TouchEvent) {
    if (dragStartY.current == null) return;
    const dy = e.touches[0].clientY - dragStartY.current;
    if (Math.abs(dy) < 24) return;
    if (dy > 60 && dragStartSnap.current === 'full') setSnap('peek');
    else if (dy > 90 && dragStartSnap.current === 'peek') onClose();
    else if (dy < -60 && dragStartSnap.current === 'peek') setSnap('full');
  }
  function onTouchEnd() {
    dragStartY.current = null;
  }

  return (
    <>
      <div
        class={`sheet-scrim ${open ? 'sheet-scrim-open' : ''}`}
        onClick={onClose}
        aria-hidden={!open}
      />
      <section
        class={`sheet ${open ? 'sheet-open' : ''} sheet-snap-${snap}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        aria-hidden={!open}
        style={{ height: `${SNAP_PCT[snap]}vh` }}
      >
        <button
          type="button"
          class="sheet-handle"
          aria-label={snap === 'peek' ? 'Expand sheet' : 'Collapse sheet'}
          onClick={() => setSnap((s) => (s === 'peek' ? 'full' : 'peek'))}
          onTouchStart={onTouchStart}
          onTouchMove={onTouchMove}
          onTouchEnd={onTouchEnd}
        >
          <span class="sheet-grab" />
        </button>
        {/* Always render a real (non-floating) header so the X has guaranteed
            space at the top of the sheet — floating-position was getting
            clipped by the iOS URL bar/notch on portrait phones. The header
            is shorter when there's no title so we still maximize body area. */}
        <div class={`sheet-header ${!title ? 'sheet-header-bare' : ''}`}>
          {title && <h2 class="sheet-title">{title}</h2>}
          <Button variant="ghost" size="sm" iconOnly onClick={onClose} aria-label="Close" title="Close">
            <Icon name="close" />
          </Button>
        </div>
        <div class="sheet-body">{children}</div>
      </section>
    </>
  );
}
