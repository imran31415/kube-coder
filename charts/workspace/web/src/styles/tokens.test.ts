import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const tokensSrc = readFileSync(join(here, 'tokens.css'), 'utf8');

describe('design tokens', () => {
  it('defines the core palette variables', () => {
    for (const v of ['--bg', '--surface', '--border', '--text', '--accent', '--danger', '--success']) {
      expect(tokensSrc).toContain(v + ':');
    }
  });

  it('provides a light-theme override under [data-theme="light"]', () => {
    expect(tokensSrc).toMatch(/\[data-theme="light"\]/);
  });

  it('respects prefers-reduced-motion', () => {
    expect(tokensSrc).toMatch(/prefers-reduced-motion: reduce/);
  });

  it('uses the phosphor-green CRT accent in dark mode', () => {
    const m = tokensSrc.match(/--accent:\s*([^;]+);/);
    expect(m?.[1].trim()).toBe('#7cffb0');
  });

  it('declares topbar, bottomnav, and rail layout heights', () => {
    expect(tokensSrc).toMatch(/--topbar-h:/);
    expect(tokensSrc).toMatch(/--bottomnav-h:/);
    expect(tokensSrc).toMatch(/--rail-w:/);
  });
});
