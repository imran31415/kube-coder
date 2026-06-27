import { useState, useEffect, useRef } from 'preact/hooks';
import { useShortcut } from '../hooks/useShortcut';
import { useEscape } from '../hooks/useEscape';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { Button } from './primitives/Button';
import { Icon } from './Icon';
import './ShortcutsHelp.css';

const SHORTCUTS: { keys: string[]; label: string }[] = [
  { keys: ['⌘', 'K'], label: 'Open command palette' },
  { keys: ['Ctrl', 'K'], label: 'Open command palette (non-Mac)' },
  { keys: ['?'], label: 'Show keyboard shortcuts' },
  { keys: ['Esc'], label: 'Close drawer / sheet / palette' },
  { keys: ['↑', '↓'], label: 'Navigate palette results' },
  { keys: ['Enter'], label: 'Select palette result' },
];

export function ShortcutsHelp() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useShortcut({ key: '?', shift: true }, () => setOpen((o) => !o));
  useEscape(open, () => setOpen(false));
  useFocusTrap(open, ref);

  // Allow other entry points (e.g. CommandPalette action) to open this in future.
  useEffect(() => {
    const onOpen = () => setOpen(true);
    window.addEventListener('open-shortcuts', onOpen);
    return () => window.removeEventListener('open-shortcuts', onOpen);
  }, []);

  if (!open) return null;
  return (
    <div class="sh-scrim" onClick={() => setOpen(false)}>
      <div ref={ref} class="sh" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" onClick={(e) => e.stopPropagation()}>
        <div class="sh-header">
          <h2 class="sh-title">Keyboard shortcuts</h2>
          <Button variant="ghost" size="sm" iconOnly onClick={() => setOpen(false)} aria-label="Close">
            <Icon name="close" />
          </Button>
        </div>
        <ul class="sh-list">
          {SHORTCUTS.map((s, i) => (
            <li key={i} class="sh-row">
              <span class="sh-label">{s.label}</span>
              <span class="sh-keys">
                {s.keys.map((k, j) => <kbd key={j} class="sh-kbd">{k}</kbd>)}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
