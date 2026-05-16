import { useEffect, useRef, useState } from 'preact/hooks';
import { openTaskStream, getTaskOutput } from '../../api/tasks';
import './output.css';

export interface TaskOutputProps {
  taskId: string;
  /** When true, attaches to the live SSE stream. False = one-shot fetch. */
  live?: boolean;
}

export function TaskOutput({ taskId, live = true }: TaskOutputProps) {
  const [text, setText] = useState<string>('');
  const [status, setStatus] = useState<'idle' | 'connecting' | 'streaming' | 'closed' | 'error'>(
    'idle',
  );
  const preRef = useRef<HTMLPreElement | null>(null);

  useEffect(() => {
    setText('');
    setStatus('connecting');
    let es: EventSource | null = null;
    let cancelled = false;

    if (live) {
      es = openTaskStream(
        taskId,
        (chunk) => {
          if (cancelled) return;
          setStatus('streaming');
          setText(chunk);
        },
        () => {
          if (cancelled) return;
          setStatus('closed');
        },
        () => {
          if (cancelled) return;
          setStatus('error');
        },
      );
    } else {
      getTaskOutput(taskId)
        .then((r) => !cancelled && setText(r.output ?? ''))
        .catch(() => !cancelled && setStatus('error'))
        .finally(() => !cancelled && setStatus('closed'));
    }

    return () => {
      cancelled = true;
      es?.close();
    };
  }, [taskId, live]);

  useEffect(() => {
    // Auto-scroll to bottom on new content.
    const el = preRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [text]);

  return (
    <div class="task-output">
      <div class="task-output-bar muted">
        <span class={`task-output-status task-output-status-${status}`} />
        <span class="mono">{status}</span>
        <button
          type="button"
          class="task-output-action"
          onClick={() => navigator.clipboard?.writeText(text)}
        >
          Copy
        </button>
      </div>
      <pre ref={preRef} class="task-output-body mono" aria-live="polite">{text || (
        status === 'connecting' ? 'connecting…' : '(no output)'
      )}</pre>
    </div>
  );
}
