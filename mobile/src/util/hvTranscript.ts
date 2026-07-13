/**
 * Group the Hypervisor's canonical event stream (see charts/workspace/
 * hypervisor_session.py) into render-ready chat turns — the mobile port of the
 * web app's transcript.ts buildTurns(). The backend delivers structured events
 * (assistant prose, tool calls/results, errors), so there is NO screen
 * scraping: we just fold events into user bubbles and agent turns.
 */
import type { HvEvent } from '../api/types';

export type HvBlock =
  | { kind: 'prose'; text: string }
  | { kind: 'activity'; label: string; detail: string; error?: boolean }
  | { kind: 'embed'; port: number; title?: string; height?: number }
  | { kind: 'media'; mediaKind: 'image' | 'video'; path?: string; url?: string; title?: string; height?: number };

export type HvTurn =
  | { role: 'user'; text: string }
  | { role: 'agent'; blocks: HvBlock[] };

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
