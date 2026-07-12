import { describe, it, expect } from 'vitest';
import { parseAgentTranscript } from './transcript';

describe('parseAgentTranscript', () => {
  it('returns nothing for an empty / whitespace pane', () => {
    expect(parseAgentTranscript('')).toEqual([]);
    expect(parseAgentTranscript('   \n\n  ')).toEqual([]);
  });

  it('extracts assistant prose as a single text block', () => {
    const raw = [
      '⏺ CLAUDE',
      '',
      "⏺ I'll check what's running across the workspace.",
      '',
      'Everything looks healthy.',
    ].join('\n');
    const blocks = parseAgentTranscript(raw);
    const text = blocks.filter((b) => b.kind === 'text');
    expect(text.length).toBeGreaterThanOrEqual(1);
    expect(text.map((b) => (b as any).text).join(' ')).toContain("what's running");
    expect(text.map((b) => (b as any).text).join(' ')).toContain('healthy');
  });

  it('classifies a shell run as an activity chip, not prose', () => {
    const raw = [
      '⏺ Running 3 shell commands…',
      '  ⎿  $ echo "=== notable user processes ==="; ps -eo pid,pcpu',
      '     $ echo "=== tmux sessions ==="; tmux ls',
    ].join('\n');
    const blocks = parseAgentTranscript(raw);
    const act = blocks.find((b) => b.kind === 'activity') as any;
    expect(act).toBeTruthy();
    expect(act.label).toMatch(/running 3 shell commands/i);
    expect(act.detail).toContain('ps -eo');
  });

  it('labels a tool-call block by the tool name', () => {
    const raw = ['⏺ Bash(ls -la /home/dev)', '  ⎿  total 40'].join('\n');
    const [block] = parseAgentTranscript(raw);
    expect(block.kind).toBe('activity');
    expect((block as any).label).toBe('Bash command');
  });

  it('strips the interactive permission menu', () => {
    const raw = [
      '⏺ I need to run a command.',
      '',
      'Do you want to proceed?',
      '❯ 1. Yes',
      "  2. Yes, and don't ask again",
      '  3. No',
      '',
      'Esc to cancel · Tab to amend · ctrl+e to explain',
    ].join('\n');
    const blocks = parseAgentTranscript(raw);
    const joined = JSON.stringify(blocks);
    expect(joined).not.toMatch(/do you want to proceed/i);
    expect(joined).not.toMatch(/1\. Yes/);
    expect(joined).not.toMatch(/Esc to cancel/i);
    // The real assistant line survives.
    expect(joined).toMatch(/need to run a command/i);
  });

  it('drops the input frame + footer chrome at the bottom of the pane', () => {
    const raw = [
      '⏺ Done — nothing else is running.',
      '',
      '╭──────────────────────────────────────────────╮',
      '│ >                                            │',
      '╰──────────────────────────────────────────────╯',
      '  ? for shortcuts',
    ].join('\n');
    const blocks = parseAgentTranscript(raw);
    const joined = JSON.stringify(blocks);
    expect(joined).toMatch(/nothing else is running/i);
    expect(joined).not.toMatch(/shortcuts/i);
    expect(joined).not.toContain('>');
  });

  it('keeps unknown content as text rather than dropping it', () => {
    const raw = 'just some plain output line with no markers';
    const [block] = parseAgentTranscript(raw);
    expect(block.kind).toBe('text');
    expect((block as any).text).toContain('plain output');
  });
});
