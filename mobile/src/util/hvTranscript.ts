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
  | { kind: 'activity'; label: string; detail: string; error?: boolean };

export type HvTurn =
  | { role: 'user'; text: string }
  | { role: 'agent'; blocks: HvBlock[] };

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
      openAgent().blocks.push({
        kind: 'activity',
        label: toolLabel(e.tool?.name || 'tool'),
        detail: prettyInput(e.tool?.input),
      });
    } else if (e.type === 'tool_result') {
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
