import { describe, expect, it, beforeEach } from 'vitest';
import { currentPath, navigate, normalize, matchRoute, routeHref } from './router';

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
