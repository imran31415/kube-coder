import { useEffect, useState } from 'preact/hooks';
import {
  desktopItems,
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
import { Drawer } from '../../components/Drawer';
import { BottomSheet } from '../../components/BottomSheet';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { serverMode } from '../../store/server-mode';
import { getGithubFullStatus, githubDisplayName } from '../../api/github';
import { DesktopEditor } from './DesktopEditor';
import { DesktopPrompt } from './DesktopPrompt';
import { DesktopMissionStrip } from './DesktopMissionStrip';
import { DesktopDock } from './DesktopDock';
import './desktop.css';

/**
 * Desktop — the workspace home (#433): a short greeting over a centered
 * composer, a live Mission Control strip, and a compact bottom dock of
 * launcher icons. The place to start work and see work in flight.
 */
export function DesktopRoute() {
  const isMobile = useIsMobile();
  const [editing, setEditing] = useState<DesktopItem | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<DesktopItem | null>(null);
  const [name, setName] = useState<string | null>(null);

  useEffect(() => {
    void refreshDesktop();
  }, []);

  // Greeting resolves to the operator's GitHub handle (gh login, falling back
  // to the git user name); an unauthenticated visitor's 401 just leaves the
  // neutral greeting.
  useEffect(() => {
    let cancelled = false;
    getGithubFullStatus()
      .then((s) => { if (!cancelled) setName(githubDisplayName(s)); })
      .catch(() => { /* leave null → neutral greeting */ });
    return () => { cancelled = true; };
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
  const error = desktopError.value;

  return (
    <div class="route route-desktop">
      <div class="dt-page">
        <h1 class="dt-greeting">
          What are we building{name ? <span class="dt-greeting-name">, {name}</span> : ''}?
        </h1>

        {error && <div class="dt-error" role="alert">{error}</div>}

        {/* Centered composer — the hero. Suppressed for read-only visitors
            who can't start chats or builds. */}
        {!serverMode.value.readOnly && <DesktopPrompt />}

        {/* Self-hides while the queue is empty. */}
        <DesktopMissionStrip />
      </div>

      <DesktopDock
        items={items}
        onLaunch={(it) => void launchItem(it)}
        onEdit={onEdit}
        onDelete={setConfirmDelete}
        onMove={(it, dir) => void moveDesktopItem(it.id, dir)}
        onNew={onNew}
      />

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
        body={confirmDelete ? `Remove "${confirmDelete.label}" from your dock? You can re-create it any time.` : ''}
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
