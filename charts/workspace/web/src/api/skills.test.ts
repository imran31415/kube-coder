import { describe, expect, it, afterEach, vi } from 'vitest';
import { listSkills, getSkill, skillsStats, scanSkills, type SkillRecord } from './skills';

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
