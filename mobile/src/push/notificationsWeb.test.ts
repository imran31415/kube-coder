/**
 * notifications.web.ts stands in for notifications.ts in the web export
 * (the browser build and the desktop app, #833). Callers import one module
 * name and Metro picks the file per platform, so a function missing from the
 * web file would be `undefined` only on web: keep the two export lists equal.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

function exportsOf(file: string): string[] {
  const src = readFileSync(path.join(__dirname, file), 'utf8');
  const fns = [...src.matchAll(/^export (?:async )?function (\w+)/gm)].map((m) => m[1]);
  const reexports = [...src.matchAll(/^export \{([^}]+)\} from/gm)].flatMap((m) =>
    m[1].split(',').map((s) => s.trim()).filter(Boolean),
  );
  return [...fns, ...reexports].sort();
}

describe('the web push module', () => {
  it('exports what the native one exports', () => {
    expect(exportsOf('notifications.web.ts')).toEqual(exportsOf('notifications.ts'));
  });

  it('never pulls in the expo push glue', () => {
    const src = readFileSync(path.join(__dirname, 'notifications.web.ts'), 'utf8');
    expect(src).not.toMatch(/from 'expo-(notifications|device|constants)'/);
  });
});
