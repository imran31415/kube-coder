// Keep the Helm payload identical to the tested Vite artifact.
import { mkdir, readFile, writeFile } from 'node:fs/promises';
const source = new URL('../dist/index.html', import.meta.url);
const target = new URL('../../web-dist/', import.meta.url);
const html = await readFile(source);
if (html.length > 1000000) throw new Error('Controller UI exceeds the ConfigMap size budget');
await mkdir(target, { recursive: true });
await writeFile(new URL('index.html', target), html);
