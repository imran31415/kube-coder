/**
 * Group the Hypervisor's canonical event stream (see charts/workspace/
 * hypervisor_session.py) into render-ready chat turns — the mobile port of the
 * web app's transcript.ts buildTurns(). The backend delivers structured events
 * (assistant prose, tool calls/results, errors), so there is NO screen
 * scraping: we just fold events into user bubbles and agent turns.
 */
import type { HvEvent, TranscriptSource } from '../api/types';

export type HvBlock =
  | { kind: 'prose'; text: string }
  | { kind: 'activity'; label: string; detail: string; error?: boolean }
  | { kind: 'embed'; port: number; title?: string; height?: number }
  | { kind: 'media'; mediaKind: 'image' | 'video'; path?: string; url?: string; title?: string; height?: number }
  | { kind: 'file'; path: string; title?: string; height?: number }
  | { kind: 'choice'; question?: string; options: string[] };

export type HvTurn =
  | { role: 'user'; text: string }
  | { role: 'agent'; blocks: HvBlock[] };

/* ── Render-time activity grouping (#546) ────────────────────────────────────
 * The mobile port of the web transcript.ts groupActivity(): same names, same
 * threshold, same wording. Purely a render concern applied on top of
 * buildTurns(), so turnCopyText() and every other consumer keeps seeing the
 * flat block list. (HvBlock's activity has no `ok` flag — grouping only needs
 * `error`.) */

export type HvActivityBlock = Extract<HvBlock, { kind: 'activity' }>;
export type HvActivityGroupBlock = {
  kind: 'activity_group';
  /** Summary line, e.g. "Ran 6 commands" (plus " · 1 failed" when any errored). */
  label: string;
  items: HvActivityBlock[];
  errors: number;
};
export type HvRenderBlock = HvBlock | HvActivityGroupBlock;

/** Runs shorter than this render exactly as before — collapsing one or two
 *  rows would add a tap for no gain. */
export const GROUP_MIN = 3;

/** Plural summaries for the labels toolLabel() knows about. Anything else (MCP
 *  tools arrive as e.g. "show app preview") falls back to "<label> ×N". */
const GROUP_LABEL: Record<string, (n: number) => string> = {
  'Ran command': (n) => `Ran ${n} commands`,
  'Read file': (n) => `Read ${n} files`,
  'Wrote file': (n) => `Wrote ${n} files`,
  'Edited file': (n) => `Edited ${n} files`,
  Searched: (n) => `Searched ${n} times`,
  'Searched files': (n) => `Searched ${n} times`,
  'Searched the web': (n) => `Searched the web ${n} times`,
  'Ran a task': (n) => `Ran ${n} tasks`,
  'Fetched a page': (n) => `Fetched ${n} pages`,
  Error: (n) => `${n} errors`,
  Result: (n) => `${n} results`,
};

function groupLabel(items: HvActivityBlock[], errors: number): string {
  const n = items.length;
  const same = items.every((b) => b.label === items[0].label);
  const only = items[0].label;
  const base = !same
    ? `Ran ${n} tools`
    : GROUP_LABEL[only]
      ? GROUP_LABEL[only](n)
      : `${only} ×${n}`;
  // Failures are never hidden behind a bland count.
  return errors > 0 ? `${base} · ${errors} failed` : base;
}

/** Fold runs of >= GROUP_MIN consecutive activity blocks into one summary
 *  block. Any other block (prose / embed / media / file / choice) breaks the
 *  run, so a turn that interleaves narration with tool calls keeps its
 *  narrative order. Order is otherwise preserved and nothing is dropped. */
export function groupActivity(blocks: HvBlock[]): HvRenderBlock[] {
  const out: HvRenderBlock[] = [];
  let run: HvActivityBlock[] = [];
  const flush = () => {
    if (run.length >= GROUP_MIN) {
      const errors = run.filter((b) => b.error).length;
      out.push({ kind: 'activity_group', label: groupLabel(run, errors), items: run, errors });
    } else {
      out.push(...run);
    }
    run = [];
  };
  for (const b of blocks) {
    if (b.kind === 'activity') {
      run.push(b);
      continue;
    }
    flush();
    out.push(b);
  }
  flush();
  return out;
}

/** The prose of an agent turn as plain markdown — what the per-turn copy
 *  button (issue #351) puts on the clipboard. Tool chips / embeds / media are
 *  activity, not the message, so only prose blocks count. Mirrors the web
 *  transcript.ts turnCopyText(). */
export function turnCopyText(blocks: HvBlock[]): string {
  return blocks
    .filter((b): b is Extract<HvBlock, { kind: 'prose' }> => b.kind === 'prose')
    .map((b) => b.text.trim())
    .filter(Boolean)
    .join('\n\n');
}

/** True when a freshly polled transcript is content-identical to the one we
 *  already hold — the mobile port of the web store's sameTranscript() (#348,
 *  ported for #371). Each 2s poll re-fetches the full transcript, so the
 *  events array gets a fresh identity every tick even when nothing changed;
 *  assigning it unconditionally re-rendered the whole transcript (Markdown
 *  included) and re-fired the scroll-pin effect's scrollToEnd on an idle
 *  chat. Events are append-only and immutable per seq within a source, so
 *  length + last-event equality is a sufficient content proxy — it also
 *  catches the optimistic negative-seq user turn being replaced by the
 *  server event (same length, different tail seq). A source flip (capture ↔
 *  session_log) re-stamps seqs, so it always counts as changed. */
export function sameTranscript(
  prev: HvEvent[],
  next: HvEvent[],
  prevSource: TranscriptSource | null,
  nextSource: TranscriptSource | null,
): boolean {
  if (prevSource !== nextSource || prev.length !== next.length) return false;
  if (next.length === 0) return true;
  const a = prev[prev.length - 1];
  const b = next[next.length - 1];
  return a.seq === b.seq && a.type === b.type && a.text === b.text;
}

/** Turns rendered from the tail of a long transcript by default (#525). React
 *  Native's ScrollView mounts every child, so a long founder/ops chat kept
 *  every turn — and every inline WebView embed — alive until the app went
 *  sluggish and crashed. We window to the most recent turns and let the user
 *  reveal older ones a page at a time. Mirrors the web transcriptWindow.ts. */
export const TURN_WINDOW = 30;
export const TURN_WINDOW_STEP = 30;

export interface TurnWindow {
  /** Index of the first rendered turn — turns before it are hidden. */
  start: number;
  /** How many turns are hidden above the window (0 ⇒ the whole thread fits). */
  hidden: number;
}

/** Tail slice of a transcript to render. `visible` is clamped up to at least
 *  TURN_WINDOW and down to the total, so a caller can seed it at TURN_WINDOW
 *  and grow it by TURN_WINDOW_STEP with no bounds bookkeeping. Pure so it is
 *  unit-tested; shared in spirit with the web turnWindow(). */
export function turnWindow(total: number, visible: number): TurnWindow {
  const shown = Math.min(Math.max(0, total), Math.max(TURN_WINDOW, visible));
  const start = Math.max(0, total - shown);
  return { start, hidden: start };
}

/** MCP render tools whose tool_call renders inline instead of a text chip. */
const APP_PREVIEW_TOOL = 'mcp__dashboard__show_app_preview';
const MEDIA_TOOL = 'mcp__dashboard__show_media';
const FILE_TOOL = 'mcp__dashboard__show_file';

function num(v: unknown): number | undefined {
  const n = typeof v === 'string' ? Number(v) : (v as number);
  return typeof n === 'number' && Number.isFinite(n) ? n : undefined;
}
function str(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

/** Map a render tool_call to its block, or null if it isn't a render tool. */
function renderBlock(name: string, input: unknown): HvBlock | null {
  const a = (input || {}) as Record<string, unknown>;
  if (name === APP_PREVIEW_TOOL) {
    const port = num(a.port);
    if (port === undefined) return null;
    return { kind: 'embed', port, title: str(a.title), height: num(a.height) };
  }
  if (name === MEDIA_TOOL) {
    const mediaKind = a.media_kind === 'video' ? 'video' : 'image';
    const path = str(a.path);
    const url = str(a.url);
    if (!path && !url) return null;
    return { kind: 'media', mediaKind, path, url, title: str(a.title), height: num(a.height) };
  }
  if (name === FILE_TOOL) {
    const path = str(a.path);
    if (!path) return null;
    return { kind: 'file', path, title: str(a.title), height: num(a.height) };
  }
  return null;
}

function prettyInput(input: unknown): string {
  if (input == null) return '';
  if (typeof input === 'string') return input;
  if (typeof input === 'object') {
    const o = input as Record<string, unknown>;
    for (const k of ['command', 'file_path', 'path', 'query', 'pattern', 'url']) {
      if (typeof o[k] === 'string' && Object.keys(o).length <= 2) return o[k] as string;
    }
    try {
      return JSON.stringify(input, null, 2);
    } catch {
      return String(input);
    }
  }
  return String(input);
}

function toolLabel(name: string): string {
  const n = (name || 'tool').toLowerCase();
  const map: Record<string, string> = {
    bash: 'Ran command',
    read: 'Read file',
    write: 'Wrote file',
    edit: 'Edited file',
    multiedit: 'Edited file',
    grep: 'Searched',
    glob: 'Searched files',
    task: 'Ran a task',
    webfetch: 'Fetched a page',
    websearch: 'Searched the web',
  };
  if (map[n]) return map[n];
  const mcp = name.match(/^mcp__[^_]+__(.+)$/);
  if (mcp) return mcp[1].replace(/_/g, ' ');
  return name;
}

export function buildTurns(events: HvEvent[]): HvTurn[] {
  const turns: HvTurn[] = [];
  let agent: { role: 'agent'; blocks: HvBlock[] } | null = null;
  // tool_use_ids of render tool_calls — their tool_result is confirmation text
  // we swallow (the rendered block is the real output), unless it errored.
  const renderIds = new Set<string>();

  const openAgent = () => {
    if (!agent) {
      agent = { role: 'agent', blocks: [] };
      turns.push(agent);
    }
    return agent;
  };

  for (const e of events) {
    if (e.role === 'user' && e.type === 'message') {
      agent = null;
      turns.push({ role: 'user', text: e.text || '' });
      continue;
    }
    if (e.type === 'message' && (e.text || '').trim()) {
      openAgent().blocks.push({ kind: 'prose', text: e.text || '' });
    } else if (e.type === 'choice' && (e.options?.length || 0) > 0) {
      openAgent().blocks.push({ kind: 'choice', question: e.question, options: e.options || [] });
    } else if (e.type === 'tool_call') {
      const rendered = renderBlock(e.tool?.name || '', e.tool?.input);
      if (rendered) {
        if (e.tool_id) renderIds.add(e.tool_id);
        openAgent().blocks.push(rendered);
      } else {
        openAgent().blocks.push({
          kind: 'activity',
          label: toolLabel(e.tool?.name || 'tool'),
          detail: prettyInput(e.tool?.input),
        });
      }
    } else if (e.type === 'tool_result') {
      if (e.tool_use_id && renderIds.has(e.tool_use_id) && !e.is_error) continue;
      const blocks = openAgent().blocks;
      const last = [...blocks].reverse().find((b) => b.kind === 'activity') as
        | { kind: 'activity'; label: string; detail: string; error?: boolean }
        | undefined;
      const result = (e.text || '').trim();
      if (last && result) {
        last.detail = `${last.detail}\n\n— result —\n${result}`.trim();
        if (e.is_error) last.error = true;
      } else if (result) {
        blocks.push({ kind: 'activity', label: 'Result', detail: result, error: e.is_error });
      }
    } else if (e.type === 'error') {
      openAgent().blocks.push({
        kind: 'activity',
        label: 'Error',
        detail: e.text || 'unknown error',
        error: true,
      });
    }
    // 'status' events carry no chat content.
  }
  return turns;
}
