import { useCallback, useEffect, useRef, useState } from 'preact/hooks';
import type { VNode } from 'preact';
import { getTaskOutput, type TaskStatus } from '../../api/tasks';
import { sendFollowup } from '../../store/tasks';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { usePoll } from '../../hooks/usePoll';

/**
 * Walk through a string of terminal output and wrap any http(s) URL in a
 * tappable <a>. Critical on mobile where the user can't highlight text
 * inside the ttyd iframe — the Send-message tab becomes a copy-friendly
 * mirror that also surfaces auth/login links Claude prints.
 */
const LINKIFY_RE = /https?:\/\/[^\s<>"'`\[\]{}|\\^]+[^\s<>"'`\[\]{}|\\^,.;:!?]/g;
function linkify(text: string): (string | VNode)[] {
  const out: (string | VNode)[] = [];
  let lastIdx = 0;
  let m: RegExpExecArray | null;
  LINKIFY_RE.lastIndex = 0;
  while ((m = LINKIFY_RE.exec(text)) !== null) {
    if (m.index > lastIdx) out.push(text.slice(lastIdx, m.index));
    out.push(
      <a
        href={m[0]}
        target="_blank"
        rel="noopener noreferrer"
        class="mc-bubble-link"
        title="Open link in new tab"
      >
        {m[0]}
      </a>
    );
    lastIdx = m.index + m[0].length;
  }
  if (lastIdx < text.length) out.push(text.slice(lastIdx));
  return out;
}

export interface MessageChatProps {
  taskId: string;
  status: TaskStatus;
  /** Optional human-readable task name for the header line. */
  taskName?: string | null;
}

/**
 * Chat-style mirror of the task's tmux pane. While the task is `running`
 * the assistant bubble re-fetches `/api/claude/tasks/{id}/output` every
 * 3 seconds; once the task reaches a terminal status the tmux pane is
 * frozen so we fetch once more and then stop. Sending a message triggers
 * an immediate refetch plus two short follow-ups so the assistant's reply
 * appears without waiting for the next poll tick.
 */
export function MessageChat({ taskId, status }: MessageChatProps) {
  const TAIL_LINES = 80;
  const POLL_MS = 3000;
  // Treat "waiting-for-input" as alive too — task is paused on a prompt and
  // sending a follow-up is exactly how the user unblocks it.
  const isRunning = status === 'running' || status === 'waiting-for-input';

  const [latest, setLatest] = useState<string>('');
  const [loaded, setLoaded] = useState(false);
  const [lastFetched, setLastFetched] = useState<number>(0);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<string[]>([]);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const cancelledRef = useRef(false);
  const timersRef = useRef<number[]>([]);

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
    return () => {
      cancelledRef.current = true;
      // Cancel any pending follow-up refetches scheduled by onSend.
      for (const t of timersRef.current) clearTimeout(t);
      timersRef.current = [];
    };
  }, [fetchTail]);

  // Live polling — only while the task is running, paused while the tab
  // is hidden, backed off on consecutive errors.
  usePoll(fetchTail, POLL_MS, { enabled: isRunning, pauseOnHidden: true });

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
      // 3s, so schedule a couple of follow-up refetches too — tracked so
      // they're cancelled if the component unmounts before they fire.
      await fetchTail();
      timersRef.current.push(
        window.setTimeout(() => void fetchTail(), 1500),
        window.setTimeout(() => void fetchTail(), 4000),
      );
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
                {trimmedLatest ? linkify(trimmedLatest) : '(no output yet)'}
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
            isRunning
              ? (status === 'waiting-for-input'
                  ? 'Task is waiting for your input — reply here. (⌘/Ctrl+Enter to send)'
                  : 'Reply to the assistant…  (⌘/Ctrl+Enter to send)')
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
