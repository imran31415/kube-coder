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

  it('defines a valid CSS color value for --accent', () => {
    // Asserts shape, not a specific hex — the dashboard palette has
    // iterated several times (phosphor green → violet → off-white) and
    // pinning the value forced a test update on every theme commit.
    // The contract we actually care about is: --accent is set, in dark
    // mode, to a parseable hex / rgb / rgba color.
    const m = tokensSrc.match(/--accent:\s*([^;]+);/);
    const value = m?.[1].trim();
    expect(value, '--accent token is defined').toBeTruthy();
    expect(value, `--accent value "${value}" should be a hex or rgb(a) color`)
      .toMatch(/^(#[0-9a-fA-F]{3,8}|rgba?\([^)]+\))$/);
  });

  it('declares topbar, bottomnav, and rail layout heights', () => {
    expect(tokensSrc).toMatch(/--topbar-h:/);
    expect(tokensSrc).toMatch(/--bottomnav-h:/);
    expect(tokensSrc).toMatch(/--rail-w:/);
  });
});
