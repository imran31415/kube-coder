import { describe, expect, it, beforeEach } from 'vitest';
import {
  currentPath,
  navigate,
  normalize,
  matchRoute,
  routeHref,
  ROUTES,
  NAV_GROUPS,
  visibleNavGroups,
  navGroupFor,
  navLabel,
  inWorkspace,
  HOME_PATH,
  NAV_TOP,
  WORKSPACE_GROUP_ID,
  WORKSPACE_TAIL,
} from './router';

beforeEach(() => {
  window.history.replaceState({}, '', '/');
  currentPath.value = '/';
});

describe('normalize()', () => {
  it('strips /next prefix', () => {
    expect(normalize('/next/tasks')).toBe('/tasks');
    expect(normalize('/next/')).toBe('/');
    expect(normalize('/next')).toBe('/');
  });

  it('strips /oauth/next prefix from ingress paths', () => {
    expect(normalize('/oauth/next/memory')).toBe('/memory');
    expect(normalize('/oauth/next')).toBe('/');
  });

  it('passes through unprefixed paths', () => {
    expect(normalize('/tasks')).toBe('/tasks');
    expect(normalize('/')).toBe('/');
  });
});

describe('navigate()', () => {
  it('updates currentPath and pushes history state', () => {
    navigate('/memory');
    expect(currentPath.value).toBe('/memory');
    expect(window.location.pathname).toContain('/memory');
  });

  it('replaces history when replace=true', () => {
    const startLen = window.history.length;
    navigate('/tasks', true);
    // history length should be unchanged after replaceState
    expect(window.history.length).toBe(startLen);
    expect(currentPath.value).toBe('/tasks');
  });
});

describe('routeHref()', () => {
  it('returns the bare path at the root (no ingress prefix)', () => {
    window.history.replaceState({}, '', '/');
    expect(routeHref('/apps/3000')).toBe('/apps/3000');
  });

  it('carries the /oauth ingress prefix so new-tab links stay authed', () => {
    window.history.replaceState({}, '', '/oauth/hypervisor');
    expect(routeHref('/apps/3000')).toBe('/oauth/apps/3000');
  });
});

describe('top-level nav: Chat + Workspace', () => {
  it('has exactly two top-level entries, Chat then Workspace', () => {
    expect(NAV_TOP.map((t) => t.title)).toEqual(['Chat', 'Workspace']);
    expect(NAV_TOP[0].path).toBe('/hypervisor');
    expect(NAV_TOP[1].id).toBe(WORKSPACE_GROUP_ID);
  });

  it('Chat is the home route', () => {
    expect(HOME_PATH).toBe('/hypervisor');
    expect(NAV_TOP[0].path).toBe(HOME_PATH);
  });

  it('keeps the old categories as sub-headings inside Workspace', () => {
    // "Machine" is the former Workspace category, renamed so it does not
    // share a name with the container it sits in.
    expect(NAV_GROUPS.map((g) => g.title)).toEqual(['Mission Control', 'Machine', 'Knowledge']);
    expect(WORKSPACE_TAIL).toEqual(['/settings']);
  });

  it('inWorkspace covers everything except Chat', () => {
    expect(inWorkspace('/hypervisor')).toBe(false);
    for (const p of ['/mission', '/board', '/tasks', '/desktop', '/docs', '/settings']) {
      expect(inWorkspace(p), p).toBe(true);
    }
  });
});

describe('NAV_GROUPS (#267)', () => {
  it('every previously reachable route is still reachable from the nav, exactly once', () => {
    const reachable = [
      NAV_TOP[0].path,
      ...NAV_GROUPS.flatMap((g) => [...(g.landing ? [g.landing] : []), ...g.items.map((i) => i.path)]),
      ...WORKSPACE_TAIL,
    ];
    // /cto resolves (it redirects into Chat with CTO mode pre-selected) but is
    // deliberately not a destination any more (#683).
    const expected = ROUTES.map((r) => r.path).filter((p) => p !== '/cto');
    expect([...reachable].sort()).toEqual([...expected].sort());
    expect(new Set(reachable).size).toBe(reachable.length);
  });

  it('does not offer /cto as a destination (#683)', () => {
    // The AI CTO is a mode of Chat now. The path still resolves for old
    // bookmarks, but nothing in the nav points at it.
    expect(NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path))).not.toContain('/cto');
    expect(ROUTES.map((r) => r.path)).toContain('/cto');
  });

  it('visibleNavGroups hides /feed only when ctoEnabled is explicitly false (#467/#470)', () => {
    const paths = (gs: ReturnType<typeof visibleNavGroups>) =>
      gs.flatMap((g) => g.items.map((i) => i.path));
    // Default / undefined / true → the Feed is present.
    expect(paths(visibleNavGroups({}))).toContain('/feed');
    expect(paths(visibleNavGroups({ ctoEnabled: true }))).toContain('/feed');
    // Explicit false → the Feed goes (it rides the AI CTO), everything else
    // stays. Chat is NOT gated (it is not in NAV_GROUPS at all — it is the
    // top-level entry): the flag hides the Mode picker inside it (#683).
    const hidden = visibleNavGroups({ ctoEnabled: false });
    expect(paths(hidden)).not.toContain('/feed');
    expect(paths(hidden)).toContain('/tasks');
    // Pure: the module constant is not mutated.
    expect(NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path))).toContain('/feed');
  });

  it('navGroupFor resolves items and landings, and misses Chat and /settings', () => {
    expect(navGroupFor('/mission')?.id).toBe('mission');
    expect(navGroupFor('/triggers')?.id).toBe('mission');
    expect(navGroupFor('/desktop')?.id).toBe('machine');
    expect(navGroupFor('/docs')?.id).toBe('knowledge');
    expect(navGroupFor('/hypervisor')).toBeUndefined();
    expect(navGroupFor('/settings')).toBeUndefined();
  });

  it('navLabel applies display overrides and falls back to ROUTES titles', () => {
    expect(navLabel('/hypervisor')).toBe('Chat');
    expect(navLabel('/tasks')).toBe('Builds');
    expect(navLabel('/memory')).toBe('Memory');
    expect(navLabel('/settings')).toBe('Settings');
  });
});

describe('matchRoute()', () => {
  it('matches an exact top-level route', () => {
    expect(matchRoute('/memory').path).toBe('/memory');
  });

  it('falls back to the default route for unknown paths', () => {
    // Default = HOME_PATH: Chat. Was Mission Control, Desktop before that,
    // and Build pre-launcher.
    expect(matchRoute('/nonsense').path).toBe('/hypervisor');
  });

  it('treats nested paths as their top-level route (detail handled inside)', () => {
    expect(matchRoute('/tasks/abc-123').path).toBe('/tasks');
  });

  it('treats `/` as the default landing route', () => {
    expect(matchRoute('/').path).toBe('/hypervisor');
    expect(matchRoute('').path).toBe('/hypervisor');
  });

  it('keeps every old deep link resolving to its own page', () => {
    for (const r of ROUTES) expect(matchRoute(r.path).path).toBe(r.path);
    expect(matchRoute('/mission').path).toBe('/mission');
  });
});
