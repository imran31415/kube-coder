import { useEffect, useRef, useState } from 'preact/hooks';
import type { TaskStatus } from '../../api/tasks';
import { sendFollowup } from '../../store/tasks';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { serverMode } from '../../store/server-mode';
import { TerminalPane } from './TerminalPane';
import { getSessionSignals } from './sessionSignals';

export interface MessageChatProps {
  taskId: string;
  status: TaskStatus;
  /** Optional human-readable task name for the header line. */
  taskName?: string | null;
}

/**
 * Send-message tab. Embeds the live ttyd terminal directly (same attach the
 * Session tab uses) so the user sees the real session — including the
 * auto-detected link badge TerminalPane renders — and pairs it with a friendly
 * composer box below. Typing in the box and hitting Send posts a follow-up to
 * the tmux session; the result streams back in the embedded terminal. This is
 * the mobile-friendly path: composing in a normal <textarea> beats typing into
 * the ttyd iframe on a phone.
 */
export function MessageChat({ taskId, status }: MessageChatProps) {
  // Treat "waiting-for-input" as alive too — the task is paused on a prompt and
  // sending a follow-up is exactly how the user unblocks it.
  const isRunning = status === 'running' || status === 'waiting-for-input';

  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // Receive clipboard text from the TaskBar "Paste from clipboard" action and
  // drop it into the composer (appending to whatever's already typed) so the
  // user can review before sending.
  const session = getSessionSignals(taskId);
  const paste = session.pasteRequest.value;
  const lastPasteNonce = useRef<number>(paste?.nonce ?? 0);
  useEffect(() => {
    if (!paste || paste.nonce === lastPasteNonce.current) return;
    lastPasteNonce.current = paste.nonce;
    setMsg((m) => (m ? `${m}${m.endsWith('\n') ? '' : '\n'}${paste.text}` : paste.text));
    inputRef.current?.focus();
  }, [paste]);

  async function onSend(e: Event) {
    e.preventDefault();
    const text = msg.trim();
    if (!text) return;
    setBusy(true);
    setMsg('');
    try {
      await sendFollowup(taskId, text);
    } finally {
      setBusy(false);
    }
  }

  function onKey(e: KeyboardEvent) {
    // Cmd/Ctrl+Enter sends; plain Enter inserts a newline.
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void onSend(e);
    }
  }

  return (
    <div class="mc">
      <div class="mc-term">
        <TerminalPane taskId={taskId} />
      </div>

      <form class="mc-composer" onSubmit={onSend}>
        <textarea
          ref={inputRef}
          class="mc-input"
          placeholder={
            serverMode.value.readOnly
              ? 'Read-only public demo — sending follow-ups is disabled.'
              : isRunning
                ? (status === 'waiting-for-input'
                    ? 'Task is waiting for your input — reply here. (⌘/Ctrl+Enter to send)'
                    : 'Reply to the assistant…  (⌘/Ctrl+Enter to send)')
                : 'Task is no longer running; replies will be queued.'
          }
          value={msg}
          onInput={(e) => setMsg((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKey}
          // rows is just the floor — CSS min-height drives the real height so
          // it can collapse to a single line on mobile (maximizing the terminal
          // viewport) while staying multi-line on desktop.
          rows={1}
          disabled={busy || serverMode.value.readOnly}
        />
        <div class="mc-composer-actions">
          <span class="muted mc-hint">
            Sent as a follow-up to the live session above.
          </span>
          <Button
            type="submit"
            variant="primary"
            size="sm"
            disabled={busy || !msg.trim() || serverMode.value.readOnly}
            title="Send (⌘/Ctrl+Enter)"
          >
            <Icon name="play" size={12} /> Send
          </Button>
        </div>
      </form>
    </div>
  );
}
