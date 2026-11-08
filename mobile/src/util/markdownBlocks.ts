/**
 * Block-level markdown parser behind components/Markdown.tsx. Pure (no React
 * Native), so it runs in the node-only vitest setup.
 *
 * It is a pragmatic subset, not a spec-complete CommonMark: enough for what a
 * coding agent writes in chat and for the workspace's docs/*.md (#856), where
 * the dashboard SPA uses `marked` and this has to agree with it.
 */

export type MdAlign = 'left' | 'center' | 'right' | undefined;

export interface MdListItem {
  blocks: MdBlock[];
  /** Set for GFM task items: `- [ ]` is false, `- [x]` is true. */
  checked?: boolean;
}

/** One block-level element parsed from the source. */
export type MdBlock =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'list'; ordered: boolean; start: number; items: MdListItem[] }
  | { kind: 'quote'; blocks: MdBlock[] }
  | { kind: 'callout'; label: string; blocks: MdBlock[] }
  | { kind: 'table'; header: string[]; align: MdAlign[]; rows: string[][] }
  | { kind: 'rule' }
  | { kind: 'para'; text: string };

export interface ParseOptions {
  /**
   * true: a single newline inside a paragraph is a line break, like the web
   * chat's `breaks: true` — agents rarely hard-wrap, so a newline they did
   * write is meant. false: standard markdown, a newline is a space — docs are
   * hard-wrapped at ~80 columns and keeping those breaks shreds the prose.
   */
  breaks: boolean;
}

const FENCE = /^\s*```/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const RULE = /^\s*(?:[-*_])\s*(?:[-*_]\s*){2,}$/; // ---, ***, ___ (with optional spaces)
const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])(\s+)(.*)$/;
const QUOTE = /^\s*>\s?(.*)$/;
const TABLE_DELIMITER = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;
const TASK = /^\[([ xX])\]\s+(.*)$/;
const CALLOUT_OPEN = ':::scenario';
const CALLOUT_CLOSE = ':::';

function indentOf(line: string): number {
  return line.match(/^\s*/)![0].length;
}

/** Split a table row on unescaped pipes. GFM splits inside code spans too —
 *  that is why a literal pipe in a cell has to be written `\|`. */
function splitRow(line: string): string[] {
  let row = line.trim();
  if (row.startsWith('|')) row = row.slice(1);
  if (row.endsWith('|') && !row.endsWith('\\|')) row = row.slice(0, -1);
  return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'));
}

/** Is `lines[i]` the header row of a GFM table (a piped row, then a delimiter
 *  row with the same number of columns)? */
function isTableStart(lines: string[], i: number): boolean {
  const next = lines[i + 1];
  if (!lines[i].includes('|') || next === undefined) return false;
  if (!next.includes('|') || !TABLE_DELIMITER.test(next)) return false;
  return splitRow(lines[i]).length === splitRow(next).length;
}

function matchItem(line: string) {
  const m = line.match(LIST_ITEM);
  if (!m || RULE.test(line)) return null;
  const ordered = /\d/.test(m[2]);
  return {
    ordered,
    start: ordered ? parseInt(m[2], 10) : 1,
    contentIndent: m[1].length + m[2].length + m[3].length,
    text: m[4],
  };
}

/** Would `lines[i]` begin a new block, ending a paragraph or list item? */
function startsBlock(lines: string[], i: number): boolean {
  const l = lines[i];
  return (
    FENCE.test(l) ||
    HEADING.test(l) ||
    RULE.test(l) ||
    QUOTE.test(l) ||
    LIST_ITEM.test(l) ||
    isTableStart(lines, i)
  );
}

function joinLines(lines: string[], { breaks }: ParseOptions): string {
  return lines.reduce((text, raw, i) => {
    const line = raw.trim();
    if (i === 0) return line;
    if (breaks) return `${text}\n${line}`;
    // Standard markdown still honours an explicit hard break on the previous
    // line: two trailing spaces, or a trailing backslash.
    const prev = lines[i - 1];
    if (/ {2,}$/.test(prev)) return `${text}\n${line}`;
    if (prev.trimEnd().endsWith('\\')) return `${text.slice(0, -1).trimEnd()}\n${line}`;
    return `${text} ${line}`;
  }, '');
}

function skipBlanks(lines: string[], from: number): number {
  let n = from;
  while (n < lines.length && !lines[n].trim()) n++;
  return n;
}

function parseTable(lines: string[], start: number): { block: MdBlock; next: number } {
  const header = splitRow(lines[start]);
  const align: MdAlign[] = splitRow(lines[start + 1]).map((c) => {
    const left = c.startsWith(':');
    const right = c.endsWith(':');
    if (left && right) return 'center';
    if (right) return 'right';
    return left ? 'left' : undefined;
  });
  const rows: string[][] = [];
  let i = start + 2;
  while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) {
    // GFM: short rows are padded, excess cells are dropped.
    const cells = splitRow(lines[i]);
    rows.push(header.map((_, c) => cells[c] ?? ''));
    i++;
  }
  return { block: { kind: 'table', header, align, rows }, next: i };
}

/** Everything that belongs to one list item: lines indented to its content
 *  column (wrapped text, nested lists, code, tables, later paragraphs), or a
 *  lazy continuation of a wrapped line that carries no indent at all. */
function takeItemBody(
  lines: string[],
  from: number,
  item: { contentIndent: number; text: string },
): { body: string[]; next: number } {
  const body = [item.text];
  let i = from;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      const n = skipBlanks(lines, i);
      if (n >= lines.length || indentOf(lines[n]) < item.contentIndent) break;
      body.push('');
    } else if (indentOf(line) >= item.contentIndent) {
      body.push(line.slice(item.contentIndent));
    } else if (
      body[body.length - 1].trim() &&
      !startsBlock(lines, i) &&
      body.filter((l) => FENCE.test(l)).length % 2 === 0
    ) {
      body.push(line.trim());
    } else {
      break;
    }
    i++;
  }
  return { body, next: i };
}

function parseList(
  lines: string[],
  start: number,
  opts: ParseOptions,
): { block: MdBlock; next: number } {
  const first = matchItem(lines[start])!;
  const items: MdListItem[] = [];
  let i = start;

  while (i < lines.length) {
    const item = matchItem(lines[i]);
    if (!item || item.ordered !== first.ordered) break;
    const { body, next } = takeItemBody(lines, i + 1, item);
    i = next;

    const task = body[0].match(TASK);
    if (task) body[0] = task[2];
    items.push({
      blocks: parseBlocks(body.join('\n'), opts),
      ...(task ? { checked: task[1] !== ' ' } : {}),
    });

    // A blank line between siblings doesn't end the list — without this,
    // "1. a\n\n2. b" would be two lists, both numbered from 1.
    const n = skipBlanks(lines, i);
    if (n > i && n < lines.length && matchItem(lines[n])?.ordered === first.ordered) i = n;
  }

  return { block: { kind: 'list', ordered: first.ordered, start: first.start, items }, next: i };
}

function parseQuote(
  lines: string[],
  start: number,
  opts: ParseOptions,
): { block: MdBlock; next: number } {
  const body: string[] = [];
  let i = start;
  while (i < lines.length && QUOTE.test(lines[i])) {
    body.push(lines[i].match(QUOTE)![1]);
    i++;
  }
  const isCallout =
    body.length >= 2 &&
    body[0].trim() === CALLOUT_OPEN &&
    body[body.length - 1].trim() === CALLOUT_CLOSE;
  const block: MdBlock = isCallout
    ? { kind: 'callout', label: 'Scenario', blocks: parseBlocks(body.slice(1, -1).join('\n'), opts) }
    : { kind: 'quote', blocks: parseBlocks(body.join('\n'), opts) };
  return { block, next: i };
}

/** Fold raw markdown source into block-level elements. */
export function parseBlocks(src: string, opts: ParseOptions = { breaks: true }): MdBlock[] {
  const lines = src.replace(/\r\n?/g, '\n').split('\n');
  const blocks: MdBlock[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Blank line — separates blocks.
    if (!line.trim()) {
      i++;
      continue;
    }

    // Fenced code block: everything up to the closing fence, verbatim.
    if (FENCE.test(line)) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !FENCE.test(lines[i])) {
        body.push(lines[i]);
        i++;
      }
      i++; // consume closing fence (or run off the end on an unterminated block)
      blocks.push({ kind: 'code', text: body.join('\n') });
      continue;
    }

    const heading = line.match(HEADING);
    if (heading) {
      blocks.push({ kind: 'heading', level: heading[1].length, text: heading[2].trim() });
      i++;
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ kind: 'rule' });
      i++;
      continue;
    }

    const parsed = isTableStart(lines, i)
      ? parseTable(lines, i)
      : LIST_ITEM.test(line)
        ? parseList(lines, i, opts)
        : QUOTE.test(line)
          ? parseQuote(lines, i, opts)
          : null;
    if (parsed) {
      blocks.push(parsed.block);
      i = parsed.next;
      continue;
    }

    // Paragraph: consecutive plain lines until a blank line or a block starter.
    const body: string[] = [];
    while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) {
      body.push(lines[i]);
      i++;
    }
    blocks.push({ kind: 'para', text: joinLines(body, opts) });
  }

  return blocks;
}

const MIN_COLUMN = 72;
const MAX_COLUMN = 340;
const CHAR_WIDTH = 7.5;
const CELL_PADDING = 20;

/** Pixel widths for a table's columns, from the longest cell in each. React
 *  Native has no table layout, so every row has to be handed the same numbers
 *  or the columns won't line up. */
export function tableColumnWidths(header: string[], rows: string[][]): number[] {
  return header.map((_, c) => {
    const longest = Math.max(
      ...[header, ...rows].map((row) => (row[c] ?? '').replace(/[`*_~]/g, '').length),
    );
    return Math.min(MAX_COLUMN, Math.max(MIN_COLUMN, longest * CHAR_WIDTH + CELL_PADDING));
  });
}
