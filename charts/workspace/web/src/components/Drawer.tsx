import type { ComponentChildren } from 'preact';
import { useRef } from 'preact/hooks';
import { useEscape } from '../hooks/useEscape';
import { useScrollLock } from '../hooks/useScrollLock';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { Button } from './primitives/Button';
import { Icon } from './Icon';
import './Drawer.css';

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ComponentChildren;
  width?: number;
}

export function Drawer({ open, onClose, title, children, width = 420 }: DrawerProps) {
  const ref = useRef<HTMLElement | null>(null);
  useEscape(open, onClose);
  useScrollLock(open);
  useFocusTrap(open, ref);
  return (
    <>
      <div
        class={`drawer-scrim ${open ? 'drawer-scrim-open' : ''}`}
        onClick={onClose}
        aria-hidden={!open}
      />
      <aside
        ref={ref}
        class={`drawer ${open ? 'drawer-open' : ''}`}
        style={{ width: `${width}px` }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        aria-hidden={!open}
      >
        <div class="drawer-header">
          <h2 class="drawer-title">{title}</h2>
          <Button variant="ghost" size="sm" iconOnly onClick={onClose} aria-label="Close drawer">
            <Icon name="close" />
          </Button>
        </div>
        <div class="drawer-body">{children}</div>
      </aside>
    </>
  );
}
