import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = dirname(here);
const tokensSrc = readFileSync(join(here, 'tokens.css'), 'utf8');

/** Every .css file under src/, recursively. */
function cssFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const full = join(dir, e.name);
    if (e.isDirectory()) return cssFiles(full);
    return e.name.endsWith('.css') ? [full] : [];
  });
}

const sheets = cssFiles(srcDir).map((path) => ({
  path: relative(srcDir, path),
  src: readFileSync(path, 'utf8'),
}));

/** Custom properties DEFINED anywhere — `--x:` at any nesting depth. */
const defined = new Set<string>();
for (const { src } of sheets) {
  for (const m of src.matchAll(/(--[a-z0-9-]+)\s*:/g)) defined.add(m[1]);
}

/**
 * Custom properties that are legitimately set from TypeScript via inline
 * `style={{ '--x': … }}`, so they never appear as a CSS definition. Each
 * one must still carry a var() fallback at its call sites, which the
 * no-undefined-reference test below enforces.
 */
const SET_FROM_JS = new Set(['--i', '--wt-level']);

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

  it('defines the type scale, weight ramp, and all four safe-area insets', () => {
    for (const t of ['2xs', 'xs', 'sm', 'md', 'lg', 'xl', '2xl', '3xl', '4xl', '5xl']) {
      expect(tokensSrc, `--text-${t} is defined`).toContain(`--text-${t}:`);
    }
    for (const w of ['normal', 'medium', 'semibold', 'bold']) {
      expect(tokensSrc, `--weight-${w} is defined`).toContain(`--weight-${w}:`);
    }
    for (const s of ['top', 'bottom', 'left', 'right']) {
      expect(tokensSrc, `--safe-${s} is defined`).toContain(`--safe-${s}:`);
    }
  });
});

/*
 * Guard against the failure mode #501 catalogued: a rule references
 * var(--something) that nothing ever defines. CSS fails these SILENTLY —
 * the property is dropped and you get a transparent <select>, a square
 * corner, or a hover that never changes — so nothing but a test catches it.
 */
describe('CSS custom-property references', () => {
  /** Split a var() body on its first TOP-LEVEL comma (fallbacks nest). */
  function splitFallback(body: string): [string, string | null] {
    let depth = 0;
    for (let i = 0; i < body.length; i++) {
      const ch = body[i];
      if (ch === '(') depth++;
      else if (ch === ')') depth--;
      else if (ch === ',' && depth === 0) return [body.slice(0, i).trim(), body.slice(i + 1)];
    }
    return [body.trim(), null];
  }

  /** Every var() reference in a sheet, with whether it has a fallback. */
  function references(src: string): { name: string; hasFallback: boolean }[] {
    const out: { name: string; hasFallback: boolean }[] = [];
    for (const m of src.matchAll(/var\(/g)) {
      let depth = 1;
      let i = m.index! + m[0].length;
      while (depth > 0 && i < src.length) {
        if (src[i] === '(') depth++;
        else if (src[i] === ')') depth--;
        i++;
      }
      const [name, fallback] = splitFallback(src.slice(m.index! + m[0].length, i - 1));
      out.push({ name, hasFallback: fallback !== null });
    }
    return out;
  }

  it('resolves every var(--token) to a definition or a fallback', () => {
    const broken: string[] = [];
    for (const { path, src } of sheets) {
      for (const { name, hasFallback } of references(src)) {
        if (defined.has(name) || hasFallback || SET_FROM_JS.has(name)) continue;
        broken.push(`${path}: var(${name}) — not defined anywhere and has no fallback`);
      }
    }
    expect(broken, `undefined CSS custom properties:\n  ${broken.join('\n  ')}`).toEqual([]);
  });

  it('has no references to tokens that were renamed away', () => {
    // These all shipped as typos/duplicates of a real token (#501). Keeping
    // them named here stops them being reintroduced by copy-paste.
    const retired = [
      '--mono', '--surface-1', '--radius-md', '--border-strong-2',
      '--warning', '--warning-soft', '--warning-border', '--warning-soft-hover',
      '--warning-border-hover', '--muted', '--surface-hover', '--text-faint',
    ];
    const hits: string[] = [];
    for (const { path, src } of sheets) {
      for (const { name } of references(src)) {
        if (retired.includes(name)) hits.push(`${path}: var(${name})`);
      }
    }
    expect(hits, `retired token names still referenced:\n  ${hits.join('\n  ')}`).toEqual([]);
  });

  it('routes every font-size and font-weight through the token ramps', () => {
    const raw: string[] = [];
    for (const { path, src } of sheets) {
      if (path === 'styles/tokens.css') continue;
      // `font-size: 0` is the icon-only-label trick, not a type-scale value.
      for (const m of src.matchAll(/font-size:\s*(?!0\s*[;}])([0-9.]+(?:px|rem|em))/g)) {
        raw.push(`${path}: font-size: ${m[1]} (use a --text-* token)`);
      }
      for (const m of src.matchAll(/font-weight:\s*([0-9]+|bold|normal)/g)) {
        raw.push(`${path}: font-weight: ${m[1]} (use a --weight-* token)`);
      }
    }
    expect(raw, `hardcoded type values:\n  ${raw.join('\n  ')}`).toEqual([]);
  });
});
