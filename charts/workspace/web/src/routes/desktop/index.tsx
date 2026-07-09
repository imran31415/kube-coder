import { useEffect, useRef, useState } from 'preact/hooks';
import {
  desktopItems,
  desktopLoaded,
  desktopError,
  refreshDesktop,
  saveDesktopItem,
  removeDesktopItem,
  moveDesktopItem,
  launchItem,
} from '../../store/desktop';
import type { DesktopItem, DesktopItemDraft } from '../../api/desktop';
import { drawerOpen, type DrawerKey } from '../../store/ui';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { Button } from '../../components/primitives/Button';
import { Icon, type IconName } from '../../components/Icon';
import { EmptyState } from '../../components/primitives/EmptyState';
import { Drawer } from '../../components/Drawer';
import { BottomSheet } from '../../components/BottomSheet';
import { MutatorOnly } from '../../components/MutatorOnly';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { serverMode } from '../../store/server-mode';
import { DesktopEditor } from './DesktopEditor';
import { DesktopBulletin } from './DesktopBulletin';
import { DesktopPrompt } from './DesktopPrompt';
import { DesktopSection } from './DesktopSection';
import { DesktopHeader } from './DesktopHeader';
import './desktop.css';

/** Render either an emoji/text icon or a named lucide-style line icon
 *  via the SPA's built-in Icon component. Server stores "icon:NAME"
 *  for the latter; anything else renders as literal text. */
function renderIconValue(icon: string) {
  if (icon.startsWith('icon:')) {
    const name = icon.slice(5) as IconName;
    return <Icon name={name} size={30} />;
  }
  return <span class="dt-cell-icon-emoji">{icon}</span>;
}

export function DesktopRoute() {
  const isMobile = useIsMobile();
  const [editing, setEditing] = useState<DesktopItem | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<DesktopItem | null>(null);

  useEffect(() => {
    void refreshDesktop();
  }, []);

  // Bind every item's hotkey while the route is mounted. The dependency
  // on desktopItems.value ensures we re-bind when items change. Skipped
  // entirely when the user is typing in an input so we don't intercept
  // their text. Modifiers parsed naively — good enough for the small
  // set we support (cmd, ctrl, alt, shift, meta + a single key char).
  useEffect(() => {
    function isTyping(): boolean {
      const a = document.activeElement as HTMLElement | null;
      if (!a) return false;
      const tag = a.tagName;
      return tag === 'INPUT' || tag === 'TEXTAREA' || a.isContentEditable;
    }
    function matches(item: DesktopItem, e: KeyboardEvent): boolean {
      if (!item.hotkey) return false;
      const parts = item.hotkey.toLowerCase().split('+').map((s) => s.trim());
      const wantCmd = parts.includes('cmd') || parts.includes('meta');
      const wantCtrl = parts.includes('ctrl');
      const wantShift = parts.includes('shift');
      const wantAlt = parts.includes('alt') || parts.includes('option');
      const key = parts.filter((p) => !['cmd', 'meta', 'ctrl', 'shift', 'alt', 'option'].includes(p))[0] ?? '';
      if (!key) return false;
      if (wantCmd !== e.metaKey) return false;
      if (wantCtrl !== e.ctrlKey) return false;
      if (wantShift !== e.shiftKey) return false;
      if (wantAlt !== e.altKey) return false;
      return e.key.toLowerCase() === key.toLowerCase();
    }
    function onKey(e: KeyboardEvent) {
      if (isTyping()) return;
      for (const item of desktopItems.value) {
        if (matches(item, e)) {
          e.preventDefault();
          void launchItem(item);
          return;
        }
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [desktopItems.value]);

  function onNew() {
    setEditing({
      id: '',
      label: '',
      icon: '✨',
      action: { type: 'task', prompt: '', workdir: '/home/dev/kube-coder' },
    });
    drawerOpen.value = 'desktop-edit' as DrawerKey;
  }
  function onEdit(item: DesktopItem) {
    setEditing(item);
    drawerOpen.value = 'desktop-edit' as DrawerKey;
  }

  const items = desktopItems.value;
  const loaded = desktopLoaded.value;
  const error = desktopError.value;

  // Empty-space click → floating "Add icon?" affordance at the click
  // position. Replaces the prior dashed end-of-grid cell. Suppressed in
  // read-only mode (no add affordance for visitors). Dismissed on
  // outside click, Escape, or after click on the prompt itself.
  const [addPrompt, setAddPrompt] = useState<{ x: number; y: number } | null>(null);
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  function onSurfaceClick(e: MouseEvent) {
    if (serverMode.value.readOnly) return;
    // If the editor drawer / bottom sheet is open, the user is inside
    // the icon form — every click bubbles up to here, which would re-
    // open the "Add icon?" prompt and steal focus from the form. Bail
    // early in that case. data-dt-stop on children is the secondary
    // line of defense for cells, bulletin, etc.
    if (drawerOpen.value === ('desktop-edit' as DrawerKey)) return;
    // Only fire when the click is on the surface itself or empty grid —
    // not on an icon button / bulletin row. Walk up from target; if we
    // hit a `data-dt-stop` ancestor before the surface, ignore.
    let node = e.target as HTMLElement | null;
    while (node && node !== surfaceRef.current) {
      if (node.dataset && node.dataset.dtStop === 'true') return;
      node = node.parentElement;
    }
    setAddPrompt({ x: e.clientX, y: e.clientY });
  }
  useEffect(() => {
    if (!addPrompt) return;
    function onEsc(e: KeyboardEvent) { if (e.key === 'Escape') setAddPrompt(null); }
    function onAway(e: MouseEvent) {
      const el = e.target as HTMLElement;
      if (!el.closest('.dt-add-prompt')) setAddPrompt(null);
    }
    window.addEventListener('keydown', onEsc);
    window.addEventListener('mousedown', onAway);
    return () => {
      window.removeEventListener('keydown', onEsc);
      window.removeEventListener('mousedown', onAway);
    };
  }, [addPrompt]);

  return (
    <div class="route route-desktop" ref={surfaceRef} onClick={onSurfaceClick}>
      <div class="dt-page">
        <DesktopHeader />

        {error && <div class="dt-error" data-dt-stop="true" role="alert">{error}</div>}

        {/* Hero prompt composer — always the first section. Suppressed for
            read-only visitors who can't start builds. */}
        {!serverMode.value.readOnly && (
          <DesktopSection
            class="dt-section-compose"
            title="Start a build"
            icon={<Icon name="chat" size={13} />}
          >
            <DesktopPrompt />
          </DesktopSection>
        )}

        <DesktopSection
          title="Shortcuts"
          icon={<Icon name="desktop" size={13} />}
          meta={
            items.length > 0 ? (
              <MutatorOnly>
                <button type="button" class="dt-section-action" data-dt-stop="true" onClick={onNew}>
                  <Icon name="plus" size={12} /> New
                </button>
              </MutatorOnly>
            ) : undefined
          }
        >
          {loaded && items.length === 0 ? (
            <EmptyState
              icon={<Icon name="inbox" size={24} />}
              title="No icons yet"
              description="Pin a build prompt, a URL, or a shell command. Optionally bind ⌘⇧1 (or any key combo) for one-tap launch."
              action={
                <MutatorOnly>
                  <Button variant="primary" data-dt-stop="true" onClick={onNew}>
                    <Icon name="plus" size={14} /> Create your first icon
                  </Button>
                </MutatorOnly>
              }
            />
          ) : (
            <div class="dt-grid" role="list">
              {items.map((it, idx) => (
                <DesktopCell
                  key={it.id}
                  item={it}
                  first={idx === 0}
                  last={idx === items.length - 1}
                  onLaunch={() => void launchItem(it)}
                  onEdit={() => onEdit(it)}
                  onDelete={() => setConfirmDelete(it)}
                  onMoveUp={() => void moveDesktopItem(it.id, 'up')}
                  onMoveDown={() => void moveDesktopItem(it.id, 'down')}
                />
              ))}
            </div>
          )}
        </DesktopSection>

        {/* Self-hides when no builds are live; renders its own section. */}
        <DesktopBulletin />
      </div>

      {addPrompt && (
        <button
          type="button"
          class="dt-add-prompt"
          style={{
            left: `${Math.min(addPrompt.x, window.innerWidth - 140)}px`,
            top: `${Math.min(addPrompt.y, window.innerHeight - 60)}px`,
          }}
          onClick={(e) => {
            e.stopPropagation();
            setAddPrompt(null);
            onNew();
          }}
        >
          <Icon name="plus" size={14} /> Add icon?
        </button>
      )}

      {!isMobile ? (
        <Drawer
          open={drawerOpen.value === ('desktop-edit' as DrawerKey)}
          onClose={() => { drawerOpen.value = null; setEditing(null); }}
          title={editing?.id ? 'Edit icon' : 'New icon'}
          width={560}
        >
          {editing && (
            <DesktopEditor
              initial={editing}
              onCancel={() => { drawerOpen.value = null; setEditing(null); }}
              onSubmit={async (draft: DesktopItemDraft) => {
                const saved = await saveDesktopItem(draft, editing.id || null);
                if (saved) { drawerOpen.value = null; setEditing(null); }
              }}
            />
          )}
        </Drawer>
      ) : (
        <BottomSheet
          open={drawerOpen.value === ('desktop-edit' as DrawerKey)}
          onClose={() => { drawerOpen.value = null; setEditing(null); }}
          initialSnap="full"
          title={editing?.id ? 'Edit icon' : 'New icon'}
        >
          {editing && (
            <DesktopEditor
              initial={editing}
              onCancel={() => { drawerOpen.value = null; setEditing(null); }}
              onSubmit={async (draft: DesktopItemDraft) => {
                const saved = await saveDesktopItem(draft, editing.id || null);
                if (saved) { drawerOpen.value = null; setEditing(null); }
              }}
            />
          )}
        </BottomSheet>
      )}

      <ConfirmDialog
        open={!!confirmDelete}
        title="Delete icon?"
        body={confirmDelete ? `Remove "${confirmDelete.label}" from your desktop? You can re-create it any time.` : ''}
        confirmLabel="Delete"
        destructive
        onConfirm={async () => {
          if (confirmDelete) await removeDesktopItem(confirmDelete.id);
          setConfirmDelete(null);
        }}
        onCancel={() => setConfirmDelete(null)}
      />
    </div>
  );
}

function DesktopCell({
  item, first, last, onLaunch, onEdit, onDelete, onMoveUp, onMoveDown,
}: {
  item: DesktopItem;
  first: boolean;
  last: boolean;
  onLaunch: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
}) {
  const subtitle =
    item.action.type === 'task'
      ? (item.action.prompt.length > 48 ? item.action.prompt.slice(0, 48) + '…' : item.action.prompt)
      : item.action.type === 'url'
        ? item.action.url
        : item.action.command;
  return (
    <div class="dt-cell" role="listitem" data-dt-stop="true">
      <button
        type="button"
        class="dt-cell-launch"
        onClick={onLaunch}
        title={`Launch · ${item.action.type}`}
      >
        <span class="dt-cell-icon" aria-hidden="true">{renderIconValue(item.icon)}</span>
        <span class="dt-cell-label">{item.label}</span>
        <span class="dt-cell-sub muted">{subtitle}</span>
      </button>
      {item.hotkey && (
        <span class="dt-cell-hotkey mono" aria-label={`Hotkey ${item.hotkey}`}>
          {formatHotkey(item.hotkey)}
        </span>
      )}
      <MutatorOnly>
        <div class="dt-cell-actions">
          <button
            type="button"
            class="dt-icon-btn"
            onClick={onMoveUp}
            disabled={first}
            aria-label="Move up"
            title="Move up"
          >
            <Icon name="chevron-left" size={12} />
          </button>
          <button
            type="button"
            class="dt-icon-btn"
            onClick={onMoveDown}
            disabled={last}
            aria-label="Move down"
            title="Move down"
          >
            <Icon name="chevron-right" size={12} />
          </button>
          <button
            type="button"
            class="dt-icon-btn"
            onClick={onEdit}
            aria-label="Edit"
            title="Edit"
          >
            <Icon name="settings" size={12} />
          </button>
          <button
            type="button"
            class="dt-icon-btn dt-icon-btn-danger"
            onClick={onDelete}
            aria-label="Delete"
            title="Delete"
          >
            <Icon name="close" size={12} />
          </button>
        </div>
      </MutatorOnly>
    </div>
  );
}

function formatHotkey(hotkey: string): string {
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
