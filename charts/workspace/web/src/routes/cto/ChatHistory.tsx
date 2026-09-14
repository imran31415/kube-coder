import { useEffect, useRef, useState } from 'preact/hooks';
import { BottomSheet } from '../../components/BottomSheet';
import { PopoverMenu } from '../../components/PopoverMenu';
import { ConfirmDialog, PromptDialog } from '../../components/ConfirmDialog';
import { deleteThread, getThread, renameThread, restoreThread, type HypervisorThread } from '../../api/hypervisor';
import {
  activeThreadId, activeStatus, config, deletedThreads, deletedLoading,
  threads, threadsLoading, sending, newChat, openThread, refreshThreads,
  refreshDeletedThreads, getChatSelectionVersion, chatManagementBusy,
} from '../../store/hypervisor';

/** History owns UI mutations; the shared store still owns the open transcript. */
export function ChatHistory({ narrow }: { narrow: boolean }) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const [trashOpen, setTrashOpen] = useState(false);
  const [archive, setArchive] = useState<HypervisorThread | null>(null);
  const [rename, setRename] = useState<HypervisorThread | null>(null);
  const [undo, setUndo] = useState<HypervisorThread | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const disabled = busy || sending.value || chatManagementBusy.value || !!config.value?.readOnly;

  async function mutate(action: () => Promise<void>) {
    if (inFlight.current || chatManagementBusy.peek()) return;
    inFlight.current = true;
    chatManagementBusy.value = true;
    setBusy(true);
    setError(null);
    try { await action(); }
    catch (e) { if (alive.current) setError(e instanceof Error ? e.message : 'Unable to update chat. Try again.'); }
    finally {
      inFlight.current = false;
      chatManagementBusy.value = false;
      if (alive.current) setBusy(false);
    }
  }

  async function refreshHistory() {
    if (threadsLoading.peek()) return;
    if (!await refreshThreads() && alive.current) setError('Unable to load chat history. Try again.');
  }

  async function archiveChat(t: HypervisorThread) {
    const selection = getChatSelectionVersion();
    const wasActive = activeThreadId.value === t.id;
    // Recheck after confirmation: the history row may be older than the turn.
    const detail = await getThread(t.id);
    if (!alive.current) return;
    if (detail.thread.status === 'running' || (activeThreadId.value === t.id && sending.value)) {
      throw new Error('This chat is running. Wait for it to finish or stop it before archiving.');
    }
    await deleteThread(t.id);
    if (!alive.current) return;
    // Remove locally even if the subsequent refresh fails. Never reopen the
    // archived ID, and never override a selection made during this request.
    threads.value = threads.value.filter(row => row.id !== t.id);
    const fallback = wasActive && selection === getChatSelectionVersion();
    if (fallback) {
      const next = threads.value[0];
      if (next) void openThread(next.id);
      else newChat();
    }
    setUndo(t);
    const [live, trash] = await Promise.all([refreshThreads(), refreshDeletedThreads()]);
    if ((!live || !trash) && alive.current) setError('Chat archived, but history could not refresh. Open history to retry.');
  }

  async function restoreChat(t: HypervisorThread) {
    await restoreThread(t.id);
    if (!alive.current) return;
    deletedThreads.value = deletedThreads.value.filter(row => row.id !== t.id);
    if (undo?.id === t.id) setUndo(null);
    const [live, trash] = await Promise.all([refreshThreads(), refreshDeletedThreads()]);
    if ((!live || !trash) && alive.current) setError('Chat restored, but history could not refresh. Open history to retry.');
  }

  function history(close: () => void) {
    return <div class="cto-history" aria-label="CTO chat history">
      {narrow && sheetOpen && notices()}
      <p class="muted">Chats for this project. New chat keeps your previous conversations.</p>
      {threadsLoading.value && <p role="status">Loading chats…</p>}
      {!threadsLoading.value && !threads.value.length && <p>No chats yet.</p>}
      <ul class="cto-history-list">
        {threads.value.map(t => <li key={t.id} class="cto-history-row">
          <button type="button" class="cto-history-select" aria-current={activeThreadId.value === t.id ? 'true' : undefined}
            disabled={busy || sending.value} onClick={() => { void openThread(t.id); close(); }}>
            <span>{t.title || 'New chat'}</span>
            <small>{relativeTime(t.updated_at || t.created_at || 0)}{t.status === 'running' ? ' · Running' : ''}</small>
          </button>
          <button type="button" disabled={disabled} aria-label={`Rename ${t.title || 'chat'}`}
            onClick={() => { close(); setRename(t); }}>Rename</button>
          <button type="button" disabled={disabled || t.status === 'running' || (activeThreadId.value === t.id && activeStatus.value === 'running')}
            title={t.status === 'running' ? 'Wait for this chat to finish before archiving' : undefined}
            aria-label={`Archive ${t.title || 'chat'}`} onClick={() => { close(); setArchive(t); }}>Archive</button>
        </li>)}
      </ul>
      <button type="button" aria-expanded={trashOpen} onClick={() => {
        setTrashOpen(!trashOpen);
        if (!trashOpen) void refreshDeletedThreads().then(ok => {
          if (!ok && alive.current) setError('Unable to load recently deleted chats. Try again.');
        });
      }}>Recently deleted</button>
      {trashOpen && <div>
        {deletedLoading.value && <p role="status">Loading recently deleted…</p>}
        {!deletedLoading.value && !deletedThreads.value.length && <p>Nothing here.</p>}
        <ul class="cto-history-list">{deletedThreads.value.map(t => <li key={t.id} class="cto-history-row">
          <span class="cto-history-name">{t.title || 'New chat'}</span>
          <button type="button" disabled={disabled} aria-label={`Restore ${t.title || 'chat'}`}
            onClick={() => void mutate(() => restoreChat(t))}>Restore</button>
        </li>)}</ul>
      </div>}
    </div>;
  }

  function notices() {
    return <>
      {error && <div class="cto-history-notice" role="alert">{error}<button type="button" onClick={() => setError(null)} aria-label="Dismiss chat error">Dismiss</button></div>}
      {undo && <div class="cto-history-notice" role="status">Chat archived.
        <button type="button" disabled={disabled} onClick={() => void mutate(() => restoreChat(undo))}>Undo</button>
        <button type="button" onClick={() => setUndo(null)} aria-label="Dismiss archive notice">Dismiss</button>
      </div>}
    </>;
  }

  return <div class="cto-history-controls">
    <button type="button" class="cto-history-trigger" disabled={disabled} onClick={newChat}>New chat</button>
    {narrow ? <>
      <button type="button" class="cto-history-trigger" aria-expanded={sheetOpen}
        onClick={() => { setSheetOpen(true); void refreshHistory(); }}>Chat history</button>
      <BottomSheet open={sheetOpen} onClose={() => setSheetOpen(false)} title="Chat history" initialSnap="full">
        {history(() => setSheetOpen(false))}
      </BottomSheet>
    </> : <PopoverMenu width={420} trigger={props => <button {...props} type="button" class="cto-history-trigger"
      onClick={e => { props.onClick(e); if (!props['aria-expanded']) void refreshHistory(); }}>Chat history</button>}>
      {close => history(close)}
    </PopoverMenu>}
    {(!narrow || !sheetOpen) && notices()}
    <ConfirmDialog open={!!archive} title="Archive this chat?" body={`“${archive?.title || 'New chat'}” will move to Recently deleted. You can restore it later.`}
      confirmLabel="Archive" destructive onCancel={() => setArchive(null)} onConfirm={() => {
        const t = archive; setArchive(null);
        if (t) void mutate(() => archiveChat(t));
      }} />
    <PromptDialog open={!!rename} title="Rename chat" initial={rename?.title || ''} onCancel={() => setRename(null)} onConfirm={title => {
      const t = rename; setRename(null);
      if (t && title.trim()) void mutate(async () => {
        await renameThread(t.id, title.trim());
        if (alive.current) await refreshHistory();
      });
    }} />
  </div>;
}

function relativeTime(timestamp: number): string {
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - timestamp) / 60));
  if (minutes < 1) return 'Just now';
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  return `${Math.floor(minutes / 1440)}d ago`;
}
