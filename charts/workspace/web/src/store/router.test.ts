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

describe('NAV_GROUPS (#267)', () => {
  it('covers every navigable route exactly once (landing or item)', () => {
    const grouped = NAV_GROUPS.flatMap((g) => [
      ...(g.landing ? [g.landing] : []),
      ...g.items.map((i) => i.path),
    ]);
    // /settings is a standalone trailing item the Rail renders itself, not a
    // category member. /cto resolves (it redirects into Chat with CTO mode
    // pre-selected) but is deliberately not a destination any more (#683).
    const expected = ROUTES.map((r) => r.path).filter(
      (p) => p !== '/settings' && p !== '/cto',
    );
    expect([...grouped].sort()).toEqual([...expected].sort());
    expect(new Set(grouped).size).toBe(grouped.length);
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
    // stays. Chat is NOT gated: the flag now hides the Mode picker inside it,
    // not the surface itself (#683).
    const hidden = visibleNavGroups({ ctoEnabled: false });
    expect(paths(hidden)).not.toContain('/feed');
    expect(paths(hidden)).toContain('/hypervisor');
    // Pure: the module constant is not mutated.
    expect(NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path))).toContain('/feed');
  });

  it('navGroupFor resolves items and landings, and misses /settings', () => {
    expect(navGroupFor('/mission')?.id).toBe('mission');
    expect(navGroupFor('/triggers')?.id).toBe('mission');
    expect(navGroupFor('/desktop')?.id).toBe('workspace');
    expect(navGroupFor('/docs')?.id).toBe('knowledge');
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
    // Default = ROUTES[0]; currently Desktop, was Build pre-launcher.
    expect(matchRoute('/nonsense').path).toBe('/desktop');
  });

  it('treats nested paths as their top-level route (detail handled inside)', () => {
    expect(matchRoute('/tasks/abc-123').path).toBe('/tasks');
  });

  it('treats `/` as the default landing route', () => {
    expect(matchRoute('/').path).toBe('/desktop');
    expect(matchRoute('').path).toBe('/desktop');
  });
});
