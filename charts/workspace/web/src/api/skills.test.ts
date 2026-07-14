import { describe, expect, it, afterEach, vi } from 'vitest';
import {
  listSkills, getSkill, skillsStats, scanSkills, syncSkill,
  SkillSyncConflictError, type SkillRecord,
} from './skills';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

/** Capture the URL + method the api layer hits, returning `body` as JSON. */
function capture(body: unknown) {
  const calls: { url: string; method: string; body?: string }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    calls.push({ url: u, method: init?.method ?? 'GET', body: init?.body as string | undefined });
    return {
      ok: true,
      status: 200,
      statusText: '',
      headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

/** Fetch mock that returns an arbitrary status + JSON body (for error paths). */
function respond(status: number, body: unknown) {
  const calls: { url: string; method: string; body?: string }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    calls.push({ url: u, method: init?.method ?? 'GET', body: init?.body as string | undefined });
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: '',
      headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

const SKILL: SkillRecord = {
  name: 'remote-task',
  description: 'Launch a task on a remote workspace',
  body: '# Remote Task Skill',
  scope: 'project',
  systems: ['claude', 'opencode'],
  user_invocable: true,
  allowed_tools: ['Bash', 'Read'],
  argument_hint: '[prompt]',
  sources: [
    { system: 'claude', path: '/home/dev/repo/.claude/skills/remote-task/SKILL.md', scope: 'project', updated_at: 1, shadowed: false },
  ],
  fingerprint: 'abc123',
  updated_at: 1,
};

describe('skills api (#187)', () => {
  it('listSkills hits /api/skills and unwraps the list', async () => {
    const calls = capture({ skills: [SKILL], count: 1 });
    const r = await listSkills();
    expect(calls[0].url).toContain('/api/skills');
    expect(calls[0].method).toBe('GET');
    expect(r.skills).toHaveLength(1);
    expect(r.skills[0].name).toBe('remote-task');
    expect(r.skills[0].systems).toEqual(['claude', 'opencode']);
  });

  it('listSkills forwards system/scope filters as query params', async () => {
    const calls = capture({ skills: [], count: 0 });
    await listSkills({ system: 'opencode', scope: 'user' });
    expect(calls[0].url).toContain('system=opencode');
    expect(calls[0].url).toContain('scope=user');
  });

  it('listSkills tolerates missing arrays (coerces to [])', async () => {
    capture({ count: 0 });
    const r = await listSkills();
    expect(r.skills).toEqual([]);
  });

  it('listSkills coerces missing nested arrays on a record', async () => {
    capture({ skills: [{ ...SKILL, systems: undefined, allowed_tools: undefined, sources: undefined }], count: 1 });
    const r = await listSkills();
    expect(r.skills[0].systems).toEqual([]);
    expect(r.skills[0].allowed_tools).toEqual([]);
    expect(r.skills[0].sources).toEqual([]);
  });

  it('getSkill hits the name endpoint and unwraps variants', async () => {
    const calls = capture({ skill: SKILL, variants: [SKILL], divergent: false });
    const d = await getSkill('remote-task');
    expect(calls[0].url).toContain('/api/skills/remote-task');
    expect(d.skill.name).toBe('remote-task');
    expect(d.variants).toHaveLength(1);
    expect(d.divergent).toBe(false);
  });

  it('getSkill surfaces divergent variants', async () => {
    const other = { ...SKILL, fingerprint: 'def456', systems: ['opencode'] };
    capture({ skill: SKILL, variants: [SKILL, other], divergent: true });
    const d = await getSkill('remote-task');
    expect(d.divergent).toBe(true);
    expect(d.variants).toHaveLength(2);
  });

  it('getSkill URL-encodes the name', async () => {
    const calls = capture({ skill: SKILL, variants: [SKILL], divergent: false });
    await getSkill('a b');
    expect(calls[0].url).toContain('/api/skills/a%20b');
  });

  it('skillsStats GETs the stats endpoint', async () => {
    const calls = capture({ total: 3, by_system: { claude: 3 }, by_scope: { user: 3 } });
    const s = await skillsStats();
    expect(calls[0].url).toContain('/api/skills/stats');
    expect(s.total).toBe(3);
  });

  it('scanSkills POSTs the _scan endpoint', async () => {
    const calls = capture({ status: 'ok', result: { scanned: 5, changed: true } });
    await scanSkills();
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toContain('/api/skills/_scan');
  });
});

describe('syncSkill cross-tool sync (PR2)', () => {
  const INPUT = {
    source_system: 'claude',
    source_scope: 'user',
    targets: [{ system: 'opencode', scope: 'user' }],
    force: false,
  };

  it('POSTs the sync endpoint with the source + targets body', async () => {
    const calls = capture({
      name: 'remote-task',
      source_system: 'claude',
      installed: [{ system: 'opencode', scope: 'user', path: '/x/SKILL.md' }],
      failed: [],
    });
    const r = await syncSkill('remote-task', INPUT);
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toContain('/api/skills/remote-task/sync');
    const sent = JSON.parse(calls[0].body!);
    expect(sent.source_system).toBe('claude');
    expect(sent.targets).toEqual([{ system: 'opencode', scope: 'user' }]);
    expect(r.installed).toHaveLength(1);
    expect(r.failed).toHaveLength(0);
  });

  it('URL-encodes the skill name', async () => {
    const calls = capture({ name: 'a b', source_system: 'claude', installed: [], failed: [] });
    await syncSkill('a b', INPUT);
    expect(calls[0].url).toContain('/api/skills/a%20b/sync');
  });

  it('surfaces per-target failures without throwing (207-style body)', async () => {
    capture({
      name: 'remote-task',
      source_system: 'claude',
      installed: [{ system: 'opencode', scope: 'user', path: '/x/SKILL.md' }],
      failed: [{ system: 'ante', scope: 'user', error: 'provider is disabled' }],
    });
    const r = await syncSkill('remote-task', INPUT);
    expect(r.installed).toHaveLength(1);
    expect(r.failed).toHaveLength(1);
    expect(r.failed[0].system).toBe('ante');
  });

  it('throws SkillSyncConflictError on a 409 conflict, carrying the conflicts', async () => {
    respond(409, {
      code: 'conflict',
      conflicts: [{ system: 'opencode', existing_fingerprint: 'deadbeef' }],
    });
    await expect(syncSkill('remote-task', INPUT)).rejects.toBeInstanceOf(SkillSyncConflictError);
    try {
      await syncSkill('remote-task', INPUT);
      expect.unreachable('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(SkillSyncConflictError);
      expect((e as SkillSyncConflictError).conflicts).toEqual([
        { system: 'opencode', existing_fingerprint: 'deadbeef' },
      ]);
    }
  });

  it('coerces a 409 with a missing conflicts array to []', async () => {
    respond(409, { code: 'conflict' });
    try {
      await syncSkill('remote-task', INPUT);
      expect.unreachable('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(SkillSyncConflictError);
      expect((e as SkillSyncConflictError).conflicts).toEqual([]);
    }
  });

  it('re-throws a non-conflict 409 as a plain ApiError', async () => {
    respond(409, { error: 'something else' });
    await expect(syncSkill('remote-task', INPUT)).rejects.not.toBeInstanceOf(SkillSyncConflictError);
  });

  it('re-throws other error statuses (400 bad target) unchanged', async () => {
    respond(400, { error: 'bad_target', code: 'bad_target' });
    const err = await syncSkill('remote-task', INPUT).catch((e) => e);
    expect(err).not.toBeInstanceOf(SkillSyncConflictError);
    expect((err as { status?: number }).status).toBe(400);
  });

  it('re-throws a 403 readonly block unchanged', async () => {
    respond(403, { error: 'read-only mode' });
    const err = await syncSkill('remote-task', INPUT).catch((e) => e);
    expect(err).not.toBeInstanceOf(SkillSyncConflictError);
    expect((err as { status?: number }).status).toBe(403);
  });
});
