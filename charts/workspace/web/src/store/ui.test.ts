import { describe, expect, it, beforeEach } from 'vitest';
import {
  theme,
  density,
  toasts,
  pushToast,
  dismissToast,
  applyDocumentAttrs,
  collapsedRailGroups,
  autoExpandedGroup,
  isRailGroupExpanded,
  toggleRailGroup,
  expandRailGroup,
  loadCollapsedGroups,
  DEFAULT_COLLAPSED_GROUPS,
} from './ui';

beforeEach(() => {
  toasts.value = [];
  theme.value = 'system';
  density.value = 'comfortable';
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('data-density');
  collapsedRailGroups.value = [];
  autoExpandedGroup.value = null;
  localStorage.clear();
});

describe('theme & density signals', () => {
  it('default to system theme + comfortable density', () => {
    expect(theme.value).toBe('system');
    expect(density.value).toBe('comfortable');
  });

  it('writes to localStorage on change', async () => {
    theme.value = 'dark';
    density.value = 'compact';
    // Effect runs synchronously when signals change.
    const raw = localStorage.getItem('kube-coder.ui');
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw!);
    expect(parsed.theme).toBe('dark');
    expect(parsed.density).toBe('compact');
  });
});

describe('applyDocumentAttrs()', () => {
  it('removes data-theme for system, sets it for dark/light', () => {
    applyDocumentAttrs('system', 'comfortable');
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);
    expect(document.documentElement.getAttribute('data-density')).toBe('comfortable');

    applyDocumentAttrs('dark', 'compact');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(document.documentElement.getAttribute('data-density')).toBe('compact');

    applyDocumentAttrs('light', 'comfortable');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });
});

describe('collapsedRailGroups (#267)', () => {
  it('toggleRailGroup collapses then re-expands a group', () => {
    expect(collapsedRailGroups.value).toEqual([]);
    toggleRailGroup('knowledge');
    expect(collapsedRailGroups.value).toEqual(['knowledge']);
    toggleRailGroup('knowledge');
    expect(collapsedRailGroups.value).toEqual([]);
  });

  it('expandRailGroup reveals transiently without touching the stored prefs', () => {
    toggleRailGroup('workspace');
    expect(collapsedRailGroups.value).toEqual(['workspace']);

    expandRailGroup('workspace');
    // Rendered open...
    expect(isRailGroupExpanded('workspace')).toBe(true);
    // ...but the persisted preference is untouched, so the five-row default
    // survives a visit to a Workspace route.
    expect(collapsedRailGroups.value).toEqual(['workspace']);
    expect(JSON.parse(localStorage.getItem('kc.rail.groups.v1')!)).toEqual([
      'workspace',
    ]);
  });

  it('navigating away re-collapses a transiently revealed group', () => {
    toggleRailGroup('knowledge');
    expandRailGroup('knowledge');
    expect(isRailGroupExpanded('knowledge')).toBe(true);

    expandRailGroup('mission'); // moved to a route in another group
    expect(isRailGroupExpanded('knowledge')).toBe(false);

    expandRailGroup(null); // moved to a route in no group at all
    expect(autoExpandedGroup.value).toBeNull();
  });

  it('an explicit toggle clears the transient reveal', () => {
    toggleRailGroup('workspace');
    expandRailGroup('workspace');
    expect(isRailGroupExpanded('workspace')).toBe(true);

    // Collapsing the group you are standing in must visibly collapse it,
    // not be silently overridden by the auto-reveal.
    toggleRailGroup('workspace'); // -> expands (removes from collapsed)
    toggleRailGroup('workspace'); // -> collapses again
    expect(isRailGroupExpanded('workspace')).toBe(false);
  });

  it('persists under its own versioned key', () => {
    toggleRailGroup('mission');
    expect(JSON.parse(localStorage.getItem('kc.rail.groups.v1')!)).toEqual(['mission']);
  });
});

describe('rail group defaults', () => {
  // The signal is initialized once at module load, so exercise the loader
  // directly — it reads localStorage on every call.
  function load(stored: string | null): string[] {
    localStorage.clear();
    if (stored !== null) localStorage.setItem('kc.rail.groups.v1', stored);
    return loadCollapsedGroups();
  }

  it('collapses Workspace and Knowledge in a browser that never set one', () => {
    expect(load(null)).toEqual(DEFAULT_COLLAPSED_GROUPS);
  });

  it('keeps a deliberate empty preference expanded', () => {
    // A user who opened both groups stored `[]`. That is a real preference
    // and must not be overwritten by the new default.
    expect(load('[]')).toEqual([]);
  });

  it('keeps an explicit stored preference', () => {
    expect(load('["mission"]')).toEqual(['mission']);
  });

  it('falls back to the default on non-array JSON', () => {
    expect(load('{"workspace":true}')).toEqual(DEFAULT_COLLAPSED_GROUPS);
  });

  it('falls back to the default on corrupt JSON', () => {
    expect(load('not json{')).toEqual(DEFAULT_COLLAPSED_GROUPS);
  });

  it('does not hand out a shared mutable default', () => {
    const a = load(null);
    a.push('mission');
    expect(load(null)).toEqual(DEFAULT_COLLAPSED_GROUPS);
  });
});

describe('toasts', () => {
  it('pushToast adds an entry and dismissToast removes it', () => {
    const id = pushToast('hello', { kind: 'success', ttl: 0 });
    expect(toasts.value).toHaveLength(1);
    expect(toasts.value[0].message).toBe('hello');
    expect(toasts.value[0].kind).toBe('success');
    dismissToast(id);
    expect(toasts.value).toHaveLength(0);
  });

  it('auto-dismisses after ttl', async () => {
    pushToast('temp', { ttl: 30 });
    expect(toasts.value).toHaveLength(1);
    await new Promise((r) => setTimeout(r, 60));
    expect(toasts.value).toHaveLength(0);
  });
});
