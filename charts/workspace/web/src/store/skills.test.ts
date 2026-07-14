import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  skills,
  writableSystems,
  loadWritableSystems,
  syncTargetsFor,
  syncSkillToTargets,
  syncingSkill,
} from './skills';
import type { SkillRecord } from '../api/skills';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
});

function mk(name: string, systems: string[], fingerprint = 'fp'): SkillRecord {
  return {
    name, description: '', body: '#', scope: 'user', systems,
    user_invocable: false, allowed_tools: [], argument_hint: '',
    sources: [], fingerprint, updated_at: 1,
  };
}

/**
 * Route fetch by URL+method. Each route is [test, respond]. `respond` returns
 * { status, body }.
 */
function routeFetch(routes: Array<[(url: string, method: string) => boolean, () => { status: number; body: unknown }]>) {
  const calls: { url: string; method: string; body?: unknown }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push({ url: u, method, body: init?.body ? JSON.parse(init.body as string) : undefined });
    for (const [test, respond] of routes) {
      if (test(u, method)) {
        const { status, body } = respond();
        return {
          ok: status >= 200 && status < 300,
          status,
          statusText: '',
          headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }
    }
    throw new Error(`unrouted ${method} ${u}`);
  }) as unknown as typeof fetch;
  return calls;
}

describe('store/skills — sync targets (PR2)', () => {
  beforeEach(() => {
    skills.value = [];
    writableSystems.value = [];
    syncingSkill.value = false;
  });

  it('syncTargetsFor returns writable harnesses the skill is not already in', () => {
    writableSystems.value = ['ante', 'claude', 'opencode'];
    expect(syncTargetsFor(mk('x', ['claude']))).toEqual(['ante', 'opencode']);
    expect(syncTargetsFor(mk('x', ['claude', 'opencode', 'ante']))).toEqual([]);
  });

  it('syncTargetsFor falls back to systems seen in the list when writable is empty', () => {
    writableSystems.value = [];
    skills.value = [mk('a', ['claude']), mk('b', ['opencode'])];
    // skillSystems derives ['claude','opencode']; a claude-only skill → ['opencode']
    expect(syncTargetsFor(mk('a', ['claude']))).toEqual(['opencode']);
  });

  it('loadWritableSystems reads enabled providers from stats.syncer.providers', async () => {
    routeFetch([
      [(u, m) => u.includes('/api/skills/stats') && m === 'GET', () => ({
        status: 200,
        body: { total: 0, by_system: {}, by_scope: {}, syncer: { providers: { claude: true, opencode: true, antigravity: false } } },
      })],
    ]);
    await loadWritableSystems();
    expect(writableSystems.value).toEqual(['claude', 'opencode']);
  });

  it('loadWritableSystems leaves writableSystems untouched on error', async () => {
    writableSystems.value = ['claude'];
    routeFetch([
      [(u) => u.includes('/api/skills/stats'), () => ({ status: 500, body: { error: 'boom' } })],
    ]);
    await loadWritableSystems();
    expect(writableSystems.value).toEqual(['claude']);
  });
});

describe('store/skills — syncSkillToTargets (PR2)', () => {
  beforeEach(() => {
    skills.value = [];
    writableSystems.value = ['claude', 'opencode'];
    syncingSkill.value = false;
  });

  it('installs, toasts, refreshes, and clears the in-flight flag on success', async () => {
    const calls = routeFetch([
      [(u, m) => u.includes('/sync') && m === 'POST', () => ({
        status: 200,
        body: { name: 'x', source_system: 'claude', installed: [{ system: 'opencode', scope: 'user', path: '/p' }], failed: [] },
      })],
      [(u, m) => u.includes('/api/skills') && m === 'GET', () => ({
        status: 200,
        body: { skills: [mk('x', ['claude', 'opencode'])], count: 1 },
      })],
    ]);
    const outcome = await syncSkillToTargets('x', 'claude', 'user', [{ system: 'opencode', scope: 'user' }]);
    expect(outcome).toEqual({ ok: true, installed: 1 });
    expect(syncingSkill.value).toBe(false);
    // POST body carried the source + targets
    const post = calls.find((c) => c.method === 'POST');
    expect(post?.body).toMatchObject({ source_system: 'claude', source_scope: 'user', force: false });
    // refresh happened → list swapped in, row collapsed to both systems
    expect(skills.value[0].systems).toEqual(['claude', 'opencode']);
  });

  it('returns the conflict list (not throwing) on a 409', async () => {
    routeFetch([
      [(u, m) => u.includes('/sync') && m === 'POST', () => ({
        status: 409,
        body: { code: 'conflict', conflicts: [{ system: 'opencode', existing_fingerprint: 'other' }] },
      })],
    ]);
    const outcome = await syncSkillToTargets('x', 'claude', 'user', [{ system: 'opencode', scope: 'user' }]);
    expect(outcome).toEqual({ ok: false, conflicts: [{ system: 'opencode', existing_fingerprint: 'other' }] });
    expect(syncingSkill.value).toBe(false);
  });

  it('forwards force:true so a retry overwrites', async () => {
    const calls = routeFetch([
      [(u, m) => u.includes('/sync') && m === 'POST', () => ({
        status: 200,
        body: { name: 'x', source_system: 'claude', installed: [{ system: 'opencode', scope: 'user', path: '/p' }], failed: [] },
      })],
      [(u, m) => u.includes('/api/skills') && m === 'GET', () => ({ status: 200, body: { skills: [], count: 0 } })],
    ]);
    await syncSkillToTargets('x', 'claude', 'user', [{ system: 'opencode', scope: 'user' }], true);
    const post = calls.find((c) => c.method === 'POST');
    expect((post?.body as { force?: boolean })?.force).toBe(true);
  });

  it('returns an error outcome (not throwing) on a non-conflict failure', async () => {
    routeFetch([
      [(u, m) => u.includes('/sync') && m === 'POST', () => ({ status: 400, body: { error: 'bad_target' } })],
    ]);
    const outcome = await syncSkillToTargets('x', 'claude', 'user', [{ system: 'nope', scope: 'user' }]);
    expect(outcome.ok).toBe(false);
    expect('error' in outcome && outcome.error).toBeTruthy();
    expect(syncingSkill.value).toBe(false);
  });
});
