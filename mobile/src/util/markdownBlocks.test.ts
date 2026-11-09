import { describe, expect, it } from 'vitest';
import { parseBlocks, tableColumnWidths, type MdBlock } from './markdownBlocks';

const docs = { breaks: false };
const para = (text: string): MdBlock => ({ kind: 'para', text });

function list(src: string, opts = docs) {
  const [b] = parseBlocks(src, opts);
  if (b.kind !== 'list') throw new Error(`expected a list, got ${b.kind}`);
  return b;
}

describe('tables (#856)', () => {
  const src = '| Method | Path |\n|---|:--:|\n| `GET` | `/api/boards` |\n| `PUT` | `/api/boards/<id>` |';

  it('parses a GFM table instead of leaving raw pipes', () => {
    expect(parseBlocks(src, docs)).toEqual([
      {
        kind: 'table',
        header: ['Method', 'Path'],
        align: [undefined, 'center'],
        rows: [
          ['`GET`', '`/api/boards`'],
          ['`PUT`', '`/api/boards/<id>`'],
        ],
      },
    ]);
  });

  it('interrupts a paragraph with no blank line before it', () => {
    expect(parseBlocks(`Intro\n${src}`, docs).map((b) => b.kind)).toEqual(['para', 'table']);
  });

  it('keeps an escaped pipe in its cell', () => {
    const [t] = parseBlocks('| a | b |\n|---|---|\n| `x \\| y` | z |', docs);
    expect(t).toMatchObject({ rows: [['`x | y`', 'z']] });
  });

  it('pads short rows and drops excess cells', () => {
    const [t] = parseBlocks('| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |', docs);
    expect(t).toMatchObject({ rows: [['1', ''], ['1', '2']] });
  });

  it('ends at a blank line or the next block', () => {
    const blocks = parseBlocks(`${src}\n\nAfter\n\n${src}\n## Next`, docs);
    expect(blocks.map((b) => b.kind)).toEqual(['table', 'para', 'table', 'heading']);
  });

  it('does not mistake a pipe in prose, or a rule, for a table', () => {
    expect(parseBlocks('a | b\nc | d', docs)).toEqual([para('a | b c | d')]);
    expect(parseBlocks('a | b\n\n---', docs).map((b) => b.kind)).toEqual(['para', 'rule']);
  });

  it('needs matching column counts', () => {
    expect(parseBlocks('| a | b |\n|---|', docs).map((b) => b.kind)).toEqual(['para']);
  });

  it('sizes columns from the longest cell, within bounds', () => {
    const [narrow, mid, wide] = tableColumnWidths(['a', 'b', 'c'], [['x', 'y'.repeat(30), 'z'.repeat(500)]]);
    expect(narrow).toBe(72);
    expect(mid).toBe(30 * 7.5 + 20);
    expect(wide).toBe(340);
  });

  it('ignores markdown markers when sizing', () => {
    expect(tableColumnWidths(['**abcdefghijkl**'], [])).toEqual(tableColumnWidths(['abcdefghijkl'], []));
  });
});

describe('line wrapping (#856)', () => {
  const wrapped = 'Provider keys exist\nto be injected\n  at spawn.';

  it('reflows hard-wrapped prose for documents', () => {
    expect(parseBlocks(wrapped, docs)).toEqual([para('Provider keys exist to be injected at spawn.')]);
  });

  it('keeps newlines as line breaks by default, for chat', () => {
    expect(parseBlocks(wrapped)).toEqual([para('Provider keys exist\nto be injected\nat spawn.')]);
  });

  it('honours explicit hard breaks in documents', () => {
    expect(parseBlocks('one  \ntwo\\\nthree', docs)).toEqual([para('one\ntwo\nthree')]);
  });

  it('reflows inside blockquotes', () => {
    expect(parseBlocks('> a\n> b', docs)).toEqual([{ kind: 'quote', blocks: [para('a b')] }]);
  });
});

describe('lists', () => {
  it('keeps a wrapped line inside its item (#856)', () => {
    const l = list('- `token` first\n- `basic` the server composes\n  `base64(u:t)` at request\n  time.');
    expect(l.items.map((it) => it.blocks)).toEqual([
      [para('`token` first')],
      [para('`basic` the server composes `base64(u:t)` at request time.')],
    ]);
  });

  it('takes an unindented wrapped line as a lazy continuation', () => {
    expect(list('- one\ntwo').items[0].blocks).toEqual([para('one two')]);
  });

  it('nests a sub-list under its parent', () => {
    const l = list('- a\n  - b\n  - c\n- d');
    expect(l.items).toHaveLength(2);
    expect(l.items[0].blocks[1]).toMatchObject({ kind: 'list', items: [{}, {}] });
  });

  it('keeps later paragraphs and code in the item, dedented', () => {
    const l = list('1. step\n\n   more\n\n   ```\n   code\n   ```\n2. next');
    expect(l.items[0].blocks).toEqual([para('step'), para('more'), { kind: 'code', text: 'code' }]);
    expect(l.items[1].blocks).toEqual([para('next')]);
  });

  it('numbers across a blank line between items', () => {
    const blocks = parseBlocks('1. a\n\n2. b', docs);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({ kind: 'list', items: [{}, {}] });
  });

  it('keeps the start number of a list that resumes', () => {
    expect(list('3. c\n4. d').start).toBe(3);
  });

  it('does not swallow the paragraph after a list', () => {
    expect(parseBlocks('- a\n\nAfter', docs).map((b) => b.kind)).toEqual(['list', 'para']);
  });

  it('does not swallow text after a code block in an item', () => {
    const l = list('- a\n  ```\n  x\n  ```\n\nAfter');
    expect(l.items[0].blocks.map((b) => b.kind)).toEqual(['para', 'code']);
  });

  it('marks task items', () => {
    expect(list('- [ ] todo\n- [x] done\n- plain').items.map((i) => i.checked)).toEqual([false, true, undefined]);
    expect(list('- [x] done').items[0].blocks).toEqual([para('done')]);
  });

  it('breaks a run where the list kind changes', () => {
    expect(parseBlocks('- a\n1. b', docs).map((b) => b.kind)).toEqual(['list', 'list']);
  });
});

describe('quotes and callouts', () => {
  it('promotes a :::scenario block to a callout', () => {
    expect(parseBlocks('> :::scenario\n> **You** do x\n> and y\n> :::', docs)).toEqual([
      { kind: 'callout', label: 'Scenario', blocks: [para('**You** do x and y')] },
    ]);
  });

  it('leaves an unterminated marker as a plain quote', () => {
    expect(parseBlocks('> :::scenario\n> x', docs)[0].kind).toBe('quote');
  });
});

describe('unchanged chat blocks', () => {
  it('still parses headings, rules, fences and CRLF', () => {
    expect(parseBlocks('# T\r\n\r\n---\r\n\r\n```sh\r\nls | wc\r\n```')).toEqual([
      { kind: 'heading', level: 1, text: 'T' },
      { kind: 'rule' },
      { kind: 'code', text: 'ls | wc' },
    ]);
  });

  it('parses an indented block as code, across blank lines and without trailing ones', () => {
    expect(parseBlocks('Runs:\n\n    GET /a\n\n    GET /b\n\nDone', docs)).toEqual([
      para('Runs:'),
      { kind: 'code', text: 'GET /a\n\nGET /b' },
      para('Done'),
    ]);
  });

  it('keeps an indented line that continues a paragraph as text', () => {
    expect(parseBlocks('one\n    two', docs)).toEqual([para('one two')]);
  });

  it('does not treat a pipe table inside a fence as a table', () => {
    expect(parseBlocks('```\n| a |\n|---|\n```')).toEqual([{ kind: 'code', text: '| a |\n|---|' }]);
  });
});
