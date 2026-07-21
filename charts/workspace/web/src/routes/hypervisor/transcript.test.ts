import { describe, it, expect } from 'vitest';
import { buildTurns, renderMarkdown, turnCopyText, type Block, type HvEvent } from './transcript';

function ev(partial: Partial<HvEvent> & Pick<HvEvent, 'role' | 'type'>, seq: number): HvEvent {
  return { seq, ts: seq, ...partial } as HvEvent;
}

describe('buildTurns', () => {
  it('returns nothing for an empty event list', () => {
    expect(buildTurns([])).toEqual([]);
  });

  it('renders show_app_preview as an embed block', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 'p1',
           tool: { name: 'mcp__dashboard__show_app_preview', input: { port: '3000', title: 'Vite', height: 320 } } }, 1),
      ev({ role: 'system', type: 'tool_result', tool_use_id: 'p1', text: 'Embedding…' }, 2),
    ]);
    if (turns[0].role === 'agent') {
      expect(turns[0].blocks).toHaveLength(1); // result swallowed
      const b = turns[0].blocks[0];
      expect(b).toEqual({ kind: 'embed', port: 3000, title: 'Vite', height: 320 });
    }
  });

  it('renders show_media (path/image) and (url/video) as media blocks', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 'm1',
           tool: { name: 'mcp__dashboard__show_media', input: { media_kind: 'image', path: 'shot.png' } } }, 1),
      ev({ role: 'assistant', type: 'tool_call', tool_id: 'm2',
           tool: { name: 'mcp__dashboard__show_media', input: { media_kind: 'video', url: 'https://x/c.mp4' } } }, 2),
    ]);
    if (turns[0].role === 'agent') {
      expect(turns[0].blocks[0]).toEqual({ kind: 'media', mediaKind: 'image', path: 'shot.png', url: undefined, title: undefined, height: undefined });
      expect(turns[0].blocks[1]).toEqual({ kind: 'media', mediaKind: 'video', path: undefined, url: 'https://x/c.mp4', title: undefined, height: undefined });
    }
  });

  it('renders show_file as a file block (and drops it when path is missing)', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 'f1',
           tool: { name: 'mcp__dashboard__show_file', input: { path: 'docs/plan.md', title: 'Plan', height: 500 } } }, 1),
      ev({ role: 'assistant', type: 'tool_call', tool_id: 'f2',
           tool: { name: 'mcp__dashboard__show_file', input: {} } }, 2),
    ]);
    if (turns[0].role === 'agent') {
      // path-less call renders no file block; it degrades to an activity chip.
      const files = turns[0].blocks.filter((b) => b.kind === 'file');
      expect(files).toEqual([{ kind: 'file', path: 'docs/plan.md', title: 'Plan', height: 500 }]);
    }
  });

  it('keeps a render tool error visible instead of swallowing it', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 'm1',
           tool: { name: 'mcp__dashboard__show_media', input: { media_kind: 'image', path: 'x.png' } } }, 1),
      ev({ role: 'system', type: 'tool_result', tool_use_id: 'm1', is_error: true, text: 'file not found' }, 2),
    ]);
    if (turns[0].role === 'agent') {
      const hasError = turns[0].blocks.some((b) => b.kind === 'activity' && b.error);
      expect(hasError).toBe(true);
    }
  });

  it('falls back to an activity chip for an unrenderable render call (missing port)', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 'p1',
           tool: { name: 'mcp__dashboard__show_app_preview', input: {} } }, 1),
    ]);
    if (turns[0].role === 'agent') {
      expect(turns[0].blocks[0].kind).toBe('activity');
    }
  });

  it('groups a user turn then an agent prose turn', () => {
    const turns = buildTurns([
      ev({ role: 'user', type: 'message', text: 'hi' }, 1),
      ev({ role: 'assistant', type: 'message', text: 'Everything looks healthy.' }, 2),
    ]);
    expect(turns).toHaveLength(2);
    expect(turns[0]).toEqual({ role: 'user', text: 'hi' });
    expect(turns[1].role).toBe('agent');
    if (turns[1].role === 'agent') {
      expect(turns[1].blocks[0]).toEqual({ kind: 'prose', text: 'Everything looks healthy.' });
    }
  });

  it('renders a tool call as a friendly activity chip', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 't1',
           tool: { name: 'Bash', input: { command: 'ps -eo pid,pcpu' } } }, 1),
    ]);
    expect(turns).toHaveLength(1);
    if (turns[0].role === 'agent') {
      const b = turns[0].blocks[0];
      expect(b.kind).toBe('activity');
      if (b.kind === 'activity') {
        expect(b.label).toBe('Ran command');
        expect(b.detail).toContain('ps -eo');
      }
    }
  });

  it('folds a tool_result into the preceding tool call and marks it resolved', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 't1',
           tool: { name: 'Bash', input: { command: 'echo hi' } } }, 1),
      ev({ role: 'system', type: 'tool_result', tool_use_id: 't1', text: 'hi' }, 2),
    ]);
    if (turns[0].role === 'agent') {
      expect(turns[0].blocks).toHaveLength(1);
      const b = turns[0].blocks[0];
      if (b.kind === 'activity') {
        expect(b.detail).toContain('hi');
        expect(b.ok).toBe(true); // ✓ outcome distinguishes a resolved run
        expect(b.error).toBeFalsy();
      }
    }
  });

  it('marks an unresolved tool call as neither ok nor error', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 't1',
           tool: { name: 'Bash', input: { command: 'sleep 5' } } }, 1),
    ]);
    if (turns[0].role === 'agent') {
      const b = turns[0].blocks[0];
      if (b.kind === 'activity') {
        expect(b.ok).toBeFalsy();
        expect(b.error).toBeFalsy();
      }
    }
  });

  it('marks an errored tool_result as error, not ok', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 't1',
           tool: { name: 'Read', input: { file_path: '/nope' } } }, 1),
      ev({ role: 'system', type: 'tool_result', tool_use_id: 't1', is_error: true, text: 'not found' }, 2),
    ]);
    if (turns[0].role === 'agent') {
      const b = turns[0].blocks[0];
      if (b.kind === 'activity') {
        expect(b.error).toBe(true);
        expect(b.ok).toBeFalsy();
      }
    }
  });

  it('surfaces an error event as an error activity', () => {
    const turns = buildTurns([
      ev({ role: 'system', type: 'error', text: 'claude exited with code 1' }, 1),
    ]);
    if (turns[0].role === 'agent') {
      const b = turns[0].blocks[0];
      expect(b.kind).toBe('activity');
      if (b.kind === 'activity') {
        expect(b.error).toBe(true);
        expect(b.detail).toMatch(/exited with code 1/);
      }
    }
  });

  it('ignores status events (no chat content)', () => {
    const turns = buildTurns([ev({ role: 'system', type: 'status', status: 'idle' }, 1)]);
    expect(turns).toEqual([]);
  });

  it('renders a choice event as a choice block with options', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'message', text: 'Pick a database:' }, 1),
      ev({ role: 'assistant', type: 'choice', question: 'Which database?', options: ['Postgres', 'MySQL'] }, 2),
    ]);
    expect(turns).toHaveLength(1);
    if (turns[0].role === 'agent') {
      expect(turns[0].blocks[0]).toEqual({ kind: 'prose', text: 'Pick a database:' });
      expect(turns[0].blocks[1]).toEqual({ kind: 'choice', question: 'Which database?', options: ['Postgres', 'MySQL'] });
    }
  });

  it('ignores a choice event with no options', () => {
    const turns = buildTurns([ev({ role: 'assistant', type: 'choice', options: [] }, 1)]);
    expect(turns).toEqual([]);
  });

  it('maps an MCP tool name to its tool part', () => {
    const turns = buildTurns([
      ev({ role: 'assistant', type: 'tool_call', tool_id: 't1',
           tool: { name: 'mcp__dashboard__get_metrics', input: {} } }, 1),
    ]);
    if (turns[0].role === 'agent') {
      const b = turns[0].blocks[0];
      if (b.kind === 'activity') expect(b.label).toBe('get metrics');
    }
  });
});

describe('renderMarkdown code copy (issue #351)', () => {
  it('wraps a fenced code block with a Copy button that survives sanitizing', () => {
    // Leading paragraph: happy-dom + DOMPurify unwraps the FIRST element of a
    // sanitized fragment (test-env quirk only — browsers keep it), so don't
    // let the codewrap be first.
    const html = renderMarkdown('Run this:\n\n```bash\nls -la\n```');
    expect(html).toContain('class="hv-codewrap"');
    expect(html).toContain('class="hv-code-copy"');
    expect(html).toContain('<pre><code class="language-bash">ls -la');
  });

  it('escapes code content and strips a hostile language tag', () => {
    const html = renderMarkdown('x\n\n```<img src=x onerror=alert(1)>\n<script>alert(1)</script>\n```');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;script&gt;');
  });

  it('leaves inline code without a copy button', () => {
    const html = renderMarkdown('run `ls -la` now');
    expect(html).toContain('<code>ls -la</code>');
    expect(html).not.toContain('hv-code-copy');
  });
});

describe('turnCopyText', () => {
  it('joins prose blocks and skips activity/embed blocks', () => {
    const blocks: Block[] = [
      { kind: 'prose', text: 'First paragraph.' },
      { kind: 'activity', label: 'Ran command', detail: 'ls' },
      { kind: 'embed', port: 3000 },
      { kind: 'prose', text: 'Second paragraph.' },
    ];
    expect(turnCopyText(blocks)).toBe('First paragraph.\n\nSecond paragraph.');
  });

  it('returns an empty string for a turn with no prose', () => {
    expect(turnCopyText([{ kind: 'activity', label: 'Ran command', detail: 'ls' }])).toBe('');
  });
});
