import { useEffect, useState } from 'preact/hooks';
import type { DesktopItem } from '../../api/desktop';
import { serverMode } from '../../store/server-mode';
import { Icon, type IconName } from '../../components/Icon';
import { MutatorOnly } from '../../components/MutatorOnly';

/**
 * Bottom dock (#433) — the compact, macOS-style home for desktop icons.
 * Pinned to the bottom of the viewport: a centered group of icon buttons
 * with the label (and hotkey) in a hover/focus tooltip instead of
 * always-visible chrome. Edit / move / delete live in a right-click context
 * menu; "+" (add) sits at the end of the dock. Purely presentational — all
 * mutation flows stay in the parent + store/desktop. In read-only mode the
 * dock still shows and launches, but the add button and context menu are
 * suppressed (same rule as the old grid).
 */

/** Render either an emoji/text icon or a named lucide-style line icon
 *  via the SPA's built-in Icon component. Server stores "icon:NAME"
 *  for the latter; anything else renders as literal text. */
function renderIconValue(icon: string) {
  if (icon.startsWith('icon:')) {
    const name = icon.slice(5) as IconName;
    return <Icon name={name} size={22} />;
  }
  return <span class="dt-dock-emoji">{icon}</span>;
}

export function formatHotkey(hotkey: string): string {
  return hotkey
    .split('+')
    .map((p) => {
      const t = p.trim().toLowerCase();
      if (t === 'cmd' || t === 'meta') return '⌘';
      if (t === 'ctrl') return '⌃';
      if (t === 'shift') return '⇧';
      if (t === 'alt' || t === 'option') return '⌥';
      return t.length === 1 ? t.toUpperCase() : t;
    })
    .join('');
}

interface Menu {
  item: DesktopItem;
  x: number;
  y: number;
}

export function DesktopDock({
  items,
  onLaunch,
  onEdit,
  onDelete,
  onMove,
  onNew,
}: {
  items: DesktopItem[];
  onLaunch: (item: DesktopItem) => void;
  onEdit: (item: DesktopItem) => void;
  onDelete: (item: DesktopItem) => void;
  onMove: (item: DesktopItem, dir: 'up' | 'down') => void;
  onNew: () => void;
}) {
  const [menu, setMenu] = useState<Menu | null>(null);

  useEffect(() => {
    if (!menu) return;
    function onEsc(e: KeyboardEvent) {
      if (e.key === 'Escape') setMenu(null);
    }
    function onAway(e: MouseEvent) {
      const el = e.target as HTMLElement;
      if (!el.closest('.dt-dock-menu')) setMenu(null);
    }
    window.addEventListener('keydown', onEsc);
    window.addEventListener('mousedown', onAway);
    return () => {
      window.removeEventListener('keydown', onEsc);
      window.removeEventListener('mousedown', onAway);
    };
  }, [menu]);

  function onContextMenu(e: MouseEvent, item: DesktopItem) {
    if (serverMode.value.readOnly) return;
    e.preventDefault();
    setMenu({ item, x: e.clientX, y: e.clientY });
  }

  function menuAction(fn: () => void) {
    setMenu(null);
    fn();
  }

  // Nothing to show a visitor with an empty dock; owners get the "+".
  if (items.length === 0 && serverMode.value.readOnly) return null;

  const idx = menu ? items.findIndex((i) => i.id === menu.item.id) : -1;

  return (
    <nav class="dt-dock" data-dt-stop="true" aria-label="Dock">
      <div class="dt-dock-inner" role="list">
        {items.map((item) => (
          <div class="dt-dock-item" role="listitem" key={item.id}>
            <button
              type="button"
              class="dt-dock-btn"
              onClick={() => onLaunch(item)}
              onContextMenu={(e) => onContextMenu(e, item)}
              aria-label={`Launch ${item.label}`}
            >
              <span class="dt-dock-glyph" aria-hidden="true">{renderIconValue(item.icon)}</span>
            </button>
            <span class="dt-dock-tip" role="presentation">
              {item.label}
              {item.hotkey && <span class="dt-dock-tip-key mono">{formatHotkey(item.hotkey)}</span>}
            </span>
          </div>
        ))}
        <MutatorOnly>
          <div class={`dt-dock-item ${items.length > 0 ? 'dt-dock-item-add' : ''}`}>
            <button type="button" class="dt-dock-btn dt-dock-btn-add" onClick={onNew} aria-label="Add icon">
              <Icon name="plus" size={18} />
            </button>
            <span class="dt-dock-tip" role="presentation">
              {items.length === 0 ? 'Add your first icon' : 'Add icon'}
            </span>
          </div>
        </MutatorOnly>
      </div>

      {menu && (
        <div
          class="dt-dock-menu"
          role="menu"
          aria-label={`Actions for ${menu.item.label}`}
          style={{
            left: `${Math.min(menu.x, window.innerWidth - 160)}px`,
            top: `${Math.max(menu.y - 150, 8)}px`,
          }}
        >
          <button type="button" role="menuitem" onClick={() => menuAction(() => onEdit(menu.item))}>
            <Icon name="pencil" size={12} /> Edit
          </button>
          <button
            type="button"
            role="menuitem"
            disabled={idx <= 0}
            onClick={() => menuAction(() => onMove(menu.item, 'up'))}
          >
            <Icon name="chevron-left" size={12} /> Move left
          </button>
          <button
            type="button"
            role="menuitem"
            disabled={idx < 0 || idx >= items.length - 1}
            onClick={() => menuAction(() => onMove(menu.item, 'down'))}
          >
            <Icon name="chevron-right" size={12} /> Move right
          </button>
          <button
            type="button"
            role="menuitem"
            class="dt-dock-menu-danger"
            onClick={() => menuAction(() => onDelete(menu.item))}
          >
            <Icon name="trash" size={12} /> Delete
          </button>
        </div>
      )}
    </nav>
  );
}
