import { prepareTerminal, terminalUrl, setScrollMode as setScrollModeApi } from '../../api/tasks';
import { uploadFile } from '../../api/files';
import { sendFollowup } from '../../store/tasks';
import { pushToast } from '../../store/ui';
import { getSessionSignals } from './sessionSignals';

/**
 * Action helpers shared by the TaskDetail settings menu and (history) the
 * TerminalPane's own top bar. Pulling them out keeps the menu thin and
 * lets future surfaces (command palette, hotkeys) reuse the same flows.
 */

/** Open this task's terminal in its own browser tab. Synchronously opens
 *  the window inside the click handler so popup blockers don't kill it. */
export async function openTerminalInNewTab(taskId: string) {
  const win = window.open('about:blank', '_blank');
  if (win) win.opener = null;
  try {
    await prepareTerminal(taskId);
  } catch {
    /* fall through — still open the terminal so user sees an error */
  }
  const url = terminalUrl();
  if (win && !win.closed) {
    win.location.replace(url);
  } else {
    window.location.href = url;
  }
}

/** Bump the per-task reattach counter so TerminalPane re-prepares + reloads
 *  its iframe. The button itself doesn't await — TerminalPane reacts
 *  asynchronously via a signal effect. */
export function triggerReattach(taskId: string) {
  const s = getSessionSignals(taskId);
  s.reattachCounter.value = s.reattachCounter.value + 1;
}

/** Toggle tmux copy-mode for the task. Optimistically updates the
 *  scrollMode signal then rolls back on failure. */
export async function toggleScrollMode(taskId: string) {
  const s = getSessionSignals(taskId);
  const next: 'enter' | 'exit' = s.scrollMode.value ? 'exit' : 'enter';
  const optimistic = next === 'enter';
  s.scrollMode.value = optimistic;
  try {
    await setScrollModeApi(taskId, next);
    if (next === 'enter') {
      pushToast('Scroll mode on — arrows / PgUp / wheel to scroll, click again to exit.', { kind: 'info', ttl: 4500 });
    }
  } catch (err) {
    s.scrollMode.value = !optimistic;
    pushToast(err instanceof Error ? err.message : 'Could not toggle scroll mode', { kind: 'danger' });
  }
}

/** Trigger a file picker, upload the chosen file into the task's
 *  workspace, then post a follow-up to the live session pointing at the
 *  uploaded path. Hidden-input ref pattern stays where it's clicked. */
export async function uploadFileToTask(taskId: string, file: File) {
  const destDir = `uploads/${taskId}`;
  try {
    await uploadFile(file, destDir);
    const fullPath = `/home/dev/${destDir}/${file.name}`;
    pushToast(`Uploaded ${file.name} → ${fullPath}`, { kind: 'success' });
    try {
      await sendFollowup(taskId, `I uploaded a file to your workspace: ${fullPath}`);
    } catch {
      pushToast('Upload OK, but could not notify the session — paste the path manually.', { kind: 'warn' });
    }
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Upload failed', { kind: 'danger' });
  }
}
