/**
 * Render a raw terminal stream into clean, colored lines for the task-detail
 * view. The input is either a tmux pane capture (a live session) or a raw PTY
 * log (a finished session) — and the Claude TUI lays text out with *cursor
 * moves*, not spaces (e.g. `…30G` = "jump to column 30"). If you only strip the
 * escapes (what a plain ANSI parser does) the words collapse together.
 *
 * So this is a tiny single-screen terminal emulator: replay the bytes onto a
 * line buffer — printable chars + SGR colors + the cursor moves used for layout
 * (CHA `…G`, CUF `…C`, CUB `…D`, CR, tab, backspace) + line erase (`…K`) — and
 * read out colored runs. Vertical moves stay on the current line (a read-only
 * mobile view doesn't need full grid + scrollback), which keeps it small and
 * correct for the common case.
 */

export interface AnsiSeg {
  text: string;
  color?: string;
  bold?: boolean;
  dim?: boolean;
}

interface Style {
  color?: string;
  bold?: boolean;
  dim?: boolean;
}
interface Cell {
  ch: string;
  style: Style;
}

// A subset of the 16 ANSI colors, tuned for a dark terminal background.
const FG: Record<number, string> = {
  30: '#5c6370', 31: '#e06c75', 32: '#98c379', 33: '#d19a66',
  34: '#61afef', 35: '#c678dd', 36: '#56b6c2', 37: '#abb2bf',
  90: '#7f848e', 91: '#e06c75', 92: '#98c379', 93: '#e5c07b',
  94: '#61afef', 95: '#c678dd', 96: '#56b6c2', 97: '#ffffff',
};

function xterm256(n: number): string | undefined {
  if (n < 16) return FG[n < 8 ? 30 + n : 82 + n];
  if (n >= 232) {
    const v = 8 + (n - 232) * 10;
    return `rgb(${v},${v},${v})`;
  }
  const c = n - 16;
  const cube = (x: number) => (x ? x * 40 + 55 : 0);
  return `rgb(${cube(Math.floor(c / 36))},${cube(Math.floor((c % 36) / 6))},${cube(c % 6)})`;
}

function applySgr(codes: number[], st: Style) {
  if (codes.length === 0) codes = [0];
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i];
    if (c === 0) {
      st.color = undefined;
      st.bold = false;
      st.dim = false;
    } else if (c === 1) st.bold = true;
    else if (c === 2) st.dim = true;
    else if (c === 22) {
      st.bold = false;
      st.dim = false;
    } else if (c === 39) st.color = undefined;
    else if (FG[c]) st.color = FG[c];
    else if (c === 38 && codes[i + 1] === 5) {
      st.color = xterm256(codes[i + 2]);
      i += 2;
    } else if (c === 38 && codes[i + 1] === 2) {
      st.color = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`;
      i += 4;
    }
    // background + other attributes are intentionally ignored for readability
  }
}

const CSI = /^\x1b\[([0-9;?]*)([@-~])/;
const OSC = /^\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/;

export function parseAnsiLines(raw: string): AnsiSeg[][] {
  const lines: Cell[][] = [[]];
  let row = 0;
  let col = 0;
  const st: Style = {};

  const cur = () => {
    while (lines.length <= row) lines.push([]);
    return lines[row];
  };
  const put = (ch: string) => {
    const line = cur();
    while (line.length < col) line.push({ ch: ' ', style: {} });
    line[col] = { ch, style: { ...st } };
    col++;
  };

  let i = 0;
  while (i < raw.length) {
    const ch = raw[i];
    if (ch === '\x1b') {
      const rest = raw.slice(i, i + 64);
      const m = CSI.exec(rest);
      if (m) {
        const params = m[1].split(';').map((s) => (s === '' ? 0 : parseInt(s, 10)));
        const p0 = params[0] || 0;
        switch (m[2]) {
          case 'm':
            applySgr(params, st);
            break;
          case 'G': // cursor horizontal absolute
            col = Math.max(0, (params[0] || 1) - 1);
            break;
          case 'C': // cursor forward
            col += p0 || 1;
            break;
          case 'D': // cursor back
            col = Math.max(0, col - (p0 || 1));
            break;
          case 'H':
          case 'f': // cursor position (we honor the column; row stays approximate)
            col = Math.max(0, (params[1] || 1) - 1);
            break;
          case 'K': {
            // erase line: 0=to end (default), 1=to start, 2=whole line
            const line = cur();
            if (p0 === 2) lines[row] = [];
            else if (p0 === 1) for (let k = 0; k <= col && k < line.length; k++) line[k] = { ch: ' ', style: {} };
            else line.length = Math.min(line.length, col);
            break;
          }
          // other CSI (cursor up/down, erase-display, …) are ignored
        }
        i += m[0].length;
        continue;
      }
      const o = OSC.exec(rest);
      if (o) {
        i += o[0].length;
        continue;
      }
      i += 2; // unknown short escape
      continue;
    }
    if (ch === '\n') {
      row++;
      col = 0;
      cur();
      i++;
      continue;
    }
    if (ch === '\r') {
      col = 0;
      i++;
      continue;
    }
    if (ch === '\t') {
      col += 8 - (col % 8);
      i++;
      continue;
    }
    if (ch === '\b') {
      col = Math.max(0, col - 1);
      i++;
      continue;
    }
    if (ch < ' ') {
      i++;
      continue; // drop other control chars
    }
    put(ch);
    i++;
  }

  return cleanup(lines.map(toSegments));
}

function toSegments(line: Cell[]): AnsiSeg[] {
  const segs: AnsiSeg[] = [];
  let cur: AnsiSeg | null = null;
  for (const cell of line) {
    const { color, bold, dim } = cell.style;
    if (cur && cur.color === color && cur.bold === bold && cur.dim === dim) cur.text += cell.ch;
    else {
      cur = { text: cell.ch, color, bold, dim };
      segs.push(cur);
    }
  }
  // trim the grid's trailing padding spaces
  while (segs.length) {
    const last = segs[segs.length - 1];
    last.text = last.text.replace(/\s+$/, '');
    if (last.text === '') segs.pop();
    else break;
  }
  return segs;
}

const DIVIDER = /^[ \t]*[─━—–\-=_]{3,}[ \t]*$/;
const lineText = (l: AnsiSeg[]) => l.map((s) => s.text).join('');

function cleanup(lines: AnsiSeg[][]): AnsiSeg[][] {
  const out: AnsiSeg[][] = [];
  for (const line of lines) {
    const txt = lineText(line);
    if (DIVIDER.test(txt)) continue; // drop TUI divider rules
    const blank = txt.trim() === '';
    if (blank && (out.length === 0 || lineText(out[out.length - 1]).trim() === '')) continue; // collapse + trim lead
    out.push(line);
  }
  while (out.length && lineText(out[out.length - 1]).trim() === '') out.pop(); // trim trailing
  return out;
}
