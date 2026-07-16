import { describe, it, expect } from 'vitest';
import { readPendingPrompt } from './MessageChat';

// readPendingPrompt guards the untrusted `pending_prompt` shape the backend
// (server.py parse_screen_prompt) attaches to task detail. Anything malformed
// must collapse to null so the Send-message composer stays in control and no
// buttons render (issue #204).
describe('readPendingPrompt', () => {
  it('accepts a numbered choice prompt', () => {
    const r = readPendingPrompt({
      kind: 'choice',
      question: 'Do you want to proceed?',
      options: [
        { index: 1, label: 'Yes' },
        { index: 2, label: "Yes, and don't ask again" },
        { index: 3, label: 'No' },
      ],
    });
    expect(r).not.toBeNull();
    expect(r!.kind).toBe('choice');
    expect(r!.question).toBe('Do you want to proceed?');
    expect(r!.options.map((o) => o.index)).toEqual([1, 2, 3]);
  });

  it('accepts a yes/no prompt with string indices', () => {
    const r = readPendingPrompt({
      kind: 'yesno',
      question: 'Overwrite file?',
      options: [
        { index: 'y', label: 'Yes' },
        { index: 'n', label: 'No' },
      ],
    });
    expect(r!.kind).toBe('yesno');
    expect(r!.options.map((o) => o.index)).toEqual(['y', 'n']);
  });

  it('normalizes a missing question to null', () => {
    const r = readPendingPrompt({
      kind: 'choice',
      options: [
        { index: 1, label: 'A' },
        { index: 2, label: 'B' },
      ],
    });
    expect(r!.question).toBeNull();
  });

  it('drops malformed options and rejects when fewer than two remain', () => {
    const r = readPendingPrompt({
      kind: 'choice',
      options: [
        { index: 1, label: 'Yes' },
        { index: 2 }, // missing label
        { label: 'no index' }, // missing index
        null,
        'garbage',
      ],
    });
    expect(r).toBeNull();
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a string', 'choice'],
    ['unknown kind', { kind: 'freeform', options: [{ index: 1, label: 'A' }, { index: 2, label: 'B' }] }],
    ['options not an array', { kind: 'choice', options: 'nope' }],
    ['empty options', { kind: 'choice', options: [] }],
  ])('returns null for %s', (_desc, input) => {
    expect(readPendingPrompt(input)).toBeNull();
  });
});
