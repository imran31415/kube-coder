import { describe, it, expect } from 'vitest';
import { extractOutput } from './SessionPreview';

describe('extractOutput', () => {
  it('returns [] for empty / whitespace-only captures', () => {
    expect(extractOutput('', 10)).toEqual([]);
    expect(extractOutput('   \n  \n', 10)).toEqual([]);
  });

  it('drops the input box + footer hints and keeps the output above it', () => {
    const pane = [
      '● Running tests…',
      '  ✓ 12 passed',
      '',
      '╭──────────────────────────────────────╮',
      '│ >                                      │',
      '╰──────────────────────────────────────╯',
      '  auto mode on (shift+tab to cycle)',
      '  ✗ Auto-update failed',
    ].join('\n');
    expect(extractOutput(pane, 10)).toEqual(['● Running tests…', '  ✓ 12 passed']);
  });

  it('cuts the ─ rule / ❯ prompt input frame used by current Claude Code', () => {
    const pane = [
      '✻ Boondoggling… (3m 47s · ↓ 11.7k tokens)',
      '',
      '────────────────────────────────────────',
      '❯ ',
      '────────────────────────────────────────',
      '  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt',
      '                  ✘ Auto-update failed: no write permission',
    ].join('\n');
    expect(extractOutput(pane, 10)).toEqual(['✻ Boondoggling… (3m 47s · ↓ 11.7k tokens)']);
  });

  it('keeps only the last n output lines', () => {
    const pane = ['a', 'b', 'c', 'd', '╭─╮', '│ > │', '╰─╯'].join('\n');
    expect(extractOutput(pane, 2)).toEqual(['c', 'd']);
  });

  it('falls back to the raw tail when there is no input box (bash session)', () => {
    const pane = ['$ ls', 'file1', 'file2', '$ '].join('\n');
    // No box → raw tail. Trailing whitespace on the prompt line is stripped.
    expect(extractOutput(pane, 2)).toEqual(['file2', '$']);
  });

  it('does not cut on box-drawing far above the bottom (e.g. a rendered table)', () => {
    const top = ['╭─table─╮', '│ cell  │', '╰───────╯'];
    const filler = Array.from({ length: 30 }, (_, i) => `line ${i}`);
    const pane = [...top, ...filler].join('\n');
    // No input box near the bottom → raw tail, table border untouched.
    expect(extractOutput(pane, 2)).toEqual(['line 28', 'line 29']);
  });
});
