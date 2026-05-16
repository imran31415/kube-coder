import { useCallback, useEffect, useRef, useState } from 'preact/hooks';
import { getTaskOutput, type TaskStatus } from '../../api/tasks';
import { sendFollowup } from '../../store/tasks';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';

export interface MessageChatProps {
  taskId: string;
  status: TaskStatus;
  /** Optional human-readable task name for the header line. */
  taskName?: string | null;
}

/**
 * Chat-style mirror of the task's tmux pane. The assistant bubble re-fetches
 * `/api/claude/tasks/{id}/output` (which capture-panes the live tmux session,
 * see server.py:760) every 3 seconds while mounted — independent of the
 * task's reported status, because status occasionally lags reality. Sending
 * a message triggers an immediate refetch so the assistant's reply appears
 * without waiting for the next poll tick.
 */
export function MessageChat({ taskId, status }: MessageChatProps) {
  const TAIL_LINES = 80;
  const POLL_MS = 3000;

  const [latest, setLatest] = useState<string>('');
  const [loaded, setLoaded] = useState(false);
  const [lastFetched, setLastFetched] = useState<number>(0);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<string[]>([]);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const cancelledRef = useRef(false);

  const fetchTail = useCallback(async () => {
    try {
      const r = await getTaskOutput(taskId, TAIL_LINES);
      if (cancelledRef.current) return;
      setLatest(r.output ?? '');
      setLastFetched(Date.now());
      setLoaded(true);
    } catch {
      if (cancelledRef.current) return;
      setLoaded(true);
    }
  }, [taskId]);

  useEffect(() => {
    cancelledRef.current = false;
    setLoaded(false);
    void fetchTail();
    const id = window.setInterval(fetchTail, POLL_MS);
    return () => {
      cancelledRef.current = true;
      clearInterval(id);
    };
  }, [fetchTail]);

  useEffect(() => {
    // Auto-scroll to bottom whenever new content lands.
    const el = bodyRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [latest, sent.length]);

  async function onSend(e: Event) {
    e.preventDefault();
    const text = msg.trim();
    if (!text) return;
    setBusy(true);
    setSent((s) => [...s, text]);
    setMsg('');
    try {
      await sendFollowup(taskId, text);
      // Refetch right away so the assistant's response appears without
      // waiting for the next poll tick. The assistant may take longer than
      // 3s, so schedule a couple of follow-up refetches too.
      await fetchTail();
      window.setTimeout(() => void fetchTail(), 1500);
      window.setTimeout(() => void fetchTail(), 4000);
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

  const trimmedLatest = latest.replace(/\s+$/g, '');
  const secsAgo = lastFetched ? Math.max(0, Math.floor((Date.now() - lastFetched) / 1000)) : null;

  return (
    <div class="mc">
      <div class="mc-body" ref={bodyRef}>
        {!loaded ? (
          <div class="mc-loading muted">Loading latest terminal output…</div>
        ) : (
          <>
            <div class="mc-bubble mc-bubble-assistant">
              <div class="mc-bubble-meta">
                <span class="mc-bubble-author">Assistant</span>
                <span class="mc-bubble-actions">
                  <span class="mc-bubble-tag muted" title={`Last ${TAIL_LINES} lines of the tmux pane; auto-refreshes every ${POLL_MS / 1000}s`}>
                    last {TAIL_LINES} lines
                    {secsAgo != null && ` · updated ${secsAgo}s ago`}
                  </span>
                  <button
                    type="button"
                    class="mc-bubble-refresh"
                    onClick={() => void fetchTail()}
                    title="Refresh now"
                    aria-label="Refresh terminal capture"
                  >
                    <Icon name="play" size={11} />
                  </button>
                </span>
              </div>
              <pre class="mc-bubble-content mono">
                {trimmedLatest || '(no output yet)'}
              </pre>
            </div>

            {sent.map((s, i) => (
              <div key={i} class="mc-bubble mc-bubble-user">
                <div class="mc-bubble-meta">
                  <span class="mc-bubble-author">You</span>
                </div>
                <div class="mc-bubble-content">{s}</div>
              </div>
            ))}
          </>
        )}
      </div>

      <form class="mc-composer" onSubmit={onSend}>
        <textarea
          class="mc-input"
          placeholder={
            status === 'running'
              ? 'Reply to the assistant…  (⌘/Ctrl+Enter to send)'
              : 'Task is no longer running; replies will be queued.'
          }
          value={msg}
          onInput={(e) => setMsg((e.target as HTMLTextAreaElement).value)}
          onKeyDown={onKey}
          rows={3}
          disabled={busy}
        />
        <div class="mc-composer-actions">
          <span class="muted mc-hint">
            Mirrors the terminal — sent as a follow-up prompt to the same tmux session.
          </span>
          <Button
            type="submit"
            variant="primary"
            size="sm"
            disabled={busy || !msg.trim()}
            title="Send (⌘/Ctrl+Enter)"
          >
            <Icon name="play" size={12} /> Send
          </Button>
        </div>
      </form>
    </div>
  );
}
