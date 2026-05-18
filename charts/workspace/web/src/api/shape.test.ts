import { describe, it, expect } from 'vitest';
import { coerceMemoryRecord, coerceTaskSummary, coerceTaskDetail, safeArray, safeString } from './shape';

describe('safeArray / safeString', () => {
  it('safeArray returns empty array for nullish / wrong-type input', () => {
    expect(safeArray(null)).toEqual([]);
    expect(safeArray(undefined)).toEqual([]);
    expect(safeArray([1, 2, 3])).toEqual([1, 2, 3]);
  });
  it('safeString returns empty string for nullish input', () => {
    expect(safeString(null)).toBe('');
    expect(safeString(undefined)).toBe('');
    expect(safeString('hi')).toBe('hi');
  });
});

describe('coerceMemoryRecord', () => {
  it('fills array/string defaults for omitted fields', () => {
    const r = coerceMemoryRecord({});
    expect(r.tags_list).toEqual([]);
    expect(r.namespace).toBe('');
    expect(r.kind).toBe('semantic');
  });
  it('leaves importance/version/access_count undefined when missing', () => {
    const r = coerceMemoryRecord({});
    expect(r.importance).toBeUndefined();
    expect(r.version).toBeUndefined();
    expect(r.access_count).toBeUndefined();
  });
});

describe('coerceTaskDetail', () => {
  it('does NOT let wrong-shaped raw fields override the coerced base', () => {
    // Regression: previous spread order (`...base, ...r`) silently undid
    // every safeString / safeArray coercion for task_id, prompt, status,
    // name, etc. Verify the fix by feeding malformed values.
    const raw = {
      task_id: 42,               // wrong type → must be coerced to ''
      prompt: null,              // wrong type → must be coerced to ''
      status: undefined,         // missing → must default to 'unknown'
      memory_injected: 'not-an-array', // wrong type → must be coerced to []
      workdir: '/home/dev',      // valid → must pass through
      extraServerField: 'ok',    // unknown → must pass through
    };
    const d = coerceTaskDetail(raw);
    expect(d.task_id).toBe('');
    expect(d.prompt).toBe('');
    expect(d.status).toBe('unknown');
    expect(d.memory_injected).toEqual([]);
    expect(d.workdir).toBe('/home/dev');
    expect((d as unknown as Record<string, unknown>).extraServerField).toBe('ok');
  });

  it('honors well-formed values', () => {
    const d = coerceTaskDetail({
      task_id: 'abc',
      prompt: 'hi',
      status: 'running',
      memory_injected: [],
      workdir: '/x',
      session_id: 's',
      assistant: 'claude',
    });
    expect(d.task_id).toBe('abc');
    expect(d.prompt).toBe('hi');
    expect(d.status).toBe('running');
    expect(d.workdir).toBe('/x');
    expect(d.session_id).toBe('s');
    expect(d.assistant).toBe('claude');
  });
});

describe('coerceTaskSummary', () => {
  it('coerces wrong types to safe defaults', () => {
    const s = coerceTaskSummary({ task_id: 42, status: 'oops', memory_injected: 'no' });
    expect(s.task_id).toBe('');
    // 'oops' isn't in the union but we don't fail-hard; renderer can fall through.
    expect(s.status).toBe('oops');
    expect(s.memory_injected).toEqual([]);
  });
});
