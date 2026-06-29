/**
 * Parse a raw (possibly ANSI-coded) tmux pane capture into clean, colored lines
 * the task-detail view can render as <Text> spans. Uses `anser` for the SGR
 * parsing; we add the mobile cleanup (drop Claude TUI divider rules, collapse
 * blank runs, trim ends) at the line level since divider lines may be colored.
 */
import Anser from 'anser';

export interface AnsiSeg {
  text: string;
  color?: string;
  bold?: boolean;
  dim?: boolean;
}

const DIVIDER = /^[ \t]*[─━—–\-=_]{3,}[ \t]*$/;

function lineText(line: AnsiSeg[]): string {
  return line.map((s) => s.text).join('');
}

export function parseAnsiLines(raw: string): AnsiSeg[][] {
  if (!raw) return [];
  // remove_empty keeps zero-length tokens out; json gives us {content,fg,decorations}.
  const tokens = Anser.ansiToJson(raw.replace(/\r/g, ''), {
    json: true,
    remove_empty: true,
    use_classes: false,
  }) as Array<{ content: string; fg: string | null; decorations?: string[]; decoration?: string | null }>;

  const lines: AnsiSeg[][] = [];
  let cur: AnsiSeg[] = [];
  for (const t of tokens) {
    const decos = t.decorations ?? (t.decoration ? [t.decoration] : []);
    const seg: Omit<AnsiSeg, 'text'> = {
      color: t.fg ? `rgb(${t.fg})` : undefined,
      bold: decos.includes('bold'),
      dim: decos.includes('dim'),
    };
    const parts = (t.content ?? '').split('\n');
    parts.forEach((p, i) => {
      if (i > 0) {
        lines.push(cur);
        cur = [];
      }
      if (p) cur.push({ text: p, ...seg });
    });
  }
  lines.push(cur);
  return cleanup(lines);
}

function cleanup(lines: AnsiSeg[][]): AnsiSeg[][] {
  const out: AnsiSeg[][] = [];
  for (const line of lines) {
    const txt = lineText(line);
    if (DIVIDER.test(txt)) continue; // drop TUI divider rules (────/----)
    const blank = txt.trim() === '';
    // collapse blank runs to one, and trim leading blanks
    if (blank && (out.length === 0 || lineText(out[out.length - 1]).trim() === '')) continue;
    out.push(line);
  }
  while (out.length && lineText(out[out.length - 1]).trim() === '') out.pop(); // trim trailing
  return out;
}
