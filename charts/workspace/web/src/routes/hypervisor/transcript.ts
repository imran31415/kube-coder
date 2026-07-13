import { marked } from 'marked';
import DOMPurify from 'dompurify';

/**
 * Turn the Hypervisor's canonical event stream (see hypervisor_session.py) into
 * render-ready chat turns. The backend already delivers structured events —
 * assistant prose, tool calls, tool results, errors — so there is NO screen
 * scraping here: we just group events into user bubbles and agent turns and
 * render prose as markdown. Adding a CLI never touches this file; it only adds
 * a backend adapter that emits the same canonical events.
 */

export type EventRole = 'user' | 'assistant' | 'system';
export type EventType =
  | 'message'
  | 'tool_call'
  | 'tool_result'
  | 'error'
  | 'status'
  | 'choice';

export interface HvEvent {
  seq: number;
  ts: number;
  role: EventRole;
  type: EventType;
  text?: string;
  tool?: { name: string; input: unknown };
  tool_id?: string;
  tool_use_id?: string;
  is_error?: boolean;
  status?: string;
  // choice events: a multiple-choice prompt the agent emitted (backend splits a
  // ```choice fence into this — see hypervisor_session.py). Rendered as buttons.
  options?: string[];
  question?: string;
}

/** A block inside an agent turn. Besides prose + tool-activity, the agent can
 *  render rich content by calling the dashboard MCP render tools (show_app_preview
 *  / show_media) — those tool_calls become `embed` / `media` blocks here. */
export type Block =
  | { kind: 'prose'; text: string }
  | { kind: 'activity'; label: string; detail: string; error?: boolean }
  | { kind: 'embed'; port: number; title?: string; height?: number }
  | { kind: 'media'; mediaKind: 'image' | 'video'; path?: string; url?: string; title?: string; height?: number }
  | { kind: 'choice'; question?: string; options: string[] };

/** MCP render tools whose tool_call renders inline instead of a text chip. */
const APP_PREVIEW_TOOL = 'mcp__dashboard__show_app_preview';
const MEDIA_TOOL = 'mcp__dashboard__show_media';

function num(v: unknown): number | undefined {
  const n = typeof v === 'string' ? Number(v) : (v as number);
  return typeof n === 'number' && Number.isFinite(n) ? n : undefined;
}
function str(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

/** Map a render tool_call to its block, or null if it isn't a render tool. */
function renderBlock(name: string, input: unknown): Block | null {
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
  return null;
}

/** A rendered turn: a user bubble or an agent turn made of blocks. */
export type Turn =
  | { role: 'user'; text: string }
  | { role: 'agent'; blocks: Block[] };

function prettyInput(input: unknown): string {
  if (input == null) return '';
  if (typeof input === 'string') return input;
  if (typeof input === 'object') {
    const o = input as Record<string, unknown>;
    // Common single-field tools read best as their bare value.
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

/** Friendly label for a tool call, e.g. Bash -> "Ran command". */
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
  // MCP tools arrive as mcp__<server>__<tool>; surface the tool part.
  const mcp = name.match(/^mcp__[^_]+__(.+)$/);
  if (mcp) return mcp[1].replace(/_/g, ' ');
  return name;
}

/** Group the flat canonical event list into user bubbles + agent turns. */
export function buildTurns(events: HvEvent[]): Turn[] {
  const turns: Turn[] = [];
  let agent: { role: 'agent'; blocks: Block[] } | null = null;
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
      // Swallow a render tool's confirmation result (unless it failed).
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

let mdReady = false;
/** Render assistant prose as safe HTML — same marked+DOMPurify path the Docs
 *  tab uses, so bullets / `code` / **bold** look native. */
export function renderMarkdown(text: string): string {
  if (!mdReady) {
    marked.setOptions({ gfm: true, breaks: true });
    mdReady = true;
  }
  const html = marked.parse(text, { async: false }) as string;
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}
