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
        {/* When there's no title, fold the drag handle and X into one row to
            save ~50px of vertical space on mobile (was: handle + bare-header).
            Titled sheets keep the separate handle + header for clearer hierarchy. */}
        {title ? (
          <>
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
            <div class="sheet-header">
              <h2 class="sheet-title">{title}</h2>
              <Button variant="ghost" size="sm" iconOnly onClick={onClose} aria-label="Close" title="Close">
                <Icon name="close" />
              </Button>
            </div>
          </>
        ) : (
          // Touch handlers live on the grab button ONLY — not the outer row.
          // Otherwise a tap on the X picks up the natural finger drift, crosses
          // the swipe-down threshold, and collapses the sheet to peek instead
          // of firing the X's click — which reads to the user as "X doesn't work."
          <div class="sheet-handlerow">
            <button
              type="button"
              class="sheet-handle sheet-handle-inline"
              aria-label={snap === 'peek' ? 'Expand sheet' : 'Collapse sheet'}
              onClick={() => setSnap((s) => (s === 'peek' ? 'full' : 'peek'))}
              onTouchStart={onTouchStart}
              onTouchMove={onTouchMove}
              onTouchEnd={onTouchEnd}
            >
              <span class="sheet-grab" />
            </button>
            <Button variant="ghost" size="sm" iconOnly onClick={onClose} aria-label="Close" title="Close">
              <Icon name="close" />
            </Button>
          </div>
        )}
        <div class={`sheet-body ${!title ? 'sheet-body-flush' : ''}`}>{children}</div>
      </section>
    </>
  );
}
