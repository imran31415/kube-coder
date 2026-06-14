import { useCallback, useState } from 'preact/hooks';
import { getTaskOutput } from '../../api/tasks';
import { usePoll } from '../../hooks/usePoll';

export interface SessionPreviewProps {
  taskId: string;
  /**
   * Only alive sessions (running / waiting-for-input) poll. Rendering a live
   * tail for every finished task would mean one request per row on the "All"
   * filter (hundreds of sessions) — so finished rows skip the preview
   * entirely. Live sessions are exactly the ones the user wants to peek at
   * while sitting on another session's detail.
   */
  alive: boolean;
}

// Fetch a generous tail so we can drop the input box + footer and still have
// PREVIEW_LINES of real output left above it.
const TAIL_LINES = 40;
const PREVIEW_LINES = 10;
// Slightly slower than the detail mirror (3s) — this is an at-a-glance peek
// across potentially several live sessions, not the focused view.
const POLL_MS = 5000;

// Full-width horizontal rule (U+2500/U+2501) — the current Claude Code TUI
// frames its input with these top & bottom rules around the `❯` prompt.
const RULE_RE = /^[─━]{8,}$/;

/**
 * The Claude/OpenCode TUI delimits its input area + footer hints ("auto mode
 * on…", "Auto-update…") from the scrollback with box-drawing chrome. Different
 * versions render it differently — full-width ─ rules around a ❯ prompt, or a
 * rounded ╭─╮│╰─╯ box — so detect either. This chrome always sits at the
 * bottom of the pane, so a naive last-N tail shows only the footer; we cut at
 * the topmost chrome line and keep the real output above it.
 */
function isChromeBoundary(line: string): boolean {
  const t = line.trim();
  if (!t) return false;
  if (RULE_RE.test(t)) return true;
  // Rounded / square box corners used by other TUI versions.
  return /^[╭╮╰╯┌┐└┘]/.test(t) && t.includes('─');
}

/**
 * Pull the last few lines of actual output out of a raw terminal capture:
 * cut the input frame + footer, then take the trailing PREVIEW_LINES. Falls
 * back to the raw tail when there's no detectable chrome (e.g. a plain bash
 * session, or chrome filled the whole visible pane).
 */
export function extractOutput(raw: string, n: number): string[] {
  const trimmed = raw.replace(/\s+$/g, '');
  if (!trimmed) return [];
  const lines = trimmed.split('\n');
  // Scan only the bottom slice — content higher up may legitimately contain
  // box-drawing (e.g. a rendered table). Don't break: keep walking up so cut
  // lands on the TOPMOST chrome line (the input frame is 3+ lines tall), not
  // just the bottom rule.
  let cut = lines.length;
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 10); i--) {
    if (isChromeBoundary(lines[i])) cut = i;
  }
  let out = lines.slice(0, cut);
  while (out.length && !out[out.length - 1].trim()) out.pop();
  // If cutting the chrome left nothing to show, fall back to the raw tail
  // rather than an empty window.
  if (out.length === 0) out = lines;
  return out.slice(-n).map((l) => l.replace(/\s+$/g, ''));
}

/**
 * Compact, read-only tail of a session's tmux pane shown under each row in
 * the build list. Reuses the same `/output` endpoint + visibility-aware
 * polling as the detail view, so the list mirrors live activity without the
 * user having to open each session.
 */
export function SessionPreview({ taskId, alive }: SessionPreviewProps) {
  const [lines, setLines] = useState<string[]>([]);
  const [loaded, setLoaded] = useState(false);

  const fetchTail = useCallback(async () => {
    const r = await getTaskOutput(taskId, TAIL_LINES);
    setLines(extractOutput(r.output ?? '', PREVIEW_LINES));
    setLoaded(true);
  }, [taskId]);

  // enabled:false short-circuits the hook entirely for finished sessions, so
  // no request is ever made for them.
  usePoll(fetchTail, POLL_MS, { enabled: alive, pauseOnHidden: true });

  if (!alive) return null;

  return (
    <div
      class="tl-row-preview mono"
      aria-hidden="true"
      title={`Live tail of this session — refreshes every ${POLL_MS / 1000}s from the running tmux pane.`}
    >
      {!loaded ? (
        <div class="tl-row-preview-line tl-row-preview-empty">…</div>
      ) : lines.length === 0 ? (
        <div class="tl-row-preview-line tl-row-preview-empty">(no output yet)</div>
      ) : (
        lines.map((l, i) => (
          <div key={i} class="tl-row-preview-line">
            {l || ' '}
          </div>
        ))
      )}
    </div>
  );
}
