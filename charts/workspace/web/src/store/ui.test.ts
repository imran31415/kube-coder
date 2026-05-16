import { describe, expect, it, beforeEach } from 'vitest';
import {
  theme,
  density,
  toasts,
  pushToast,
  dismissToast,
  applyDocumentAttrs,
} from './ui';

beforeEach(() => {
  toasts.value = [];
  theme.value = 'system';
  density.value = 'comfortable';
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('data-density');
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
