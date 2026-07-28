import { chromium } from 'playwright-core';
import { fileURLToPath } from 'url';
import path from 'path';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const targets = process.argv.slice(2);
const b = await chromium.launch({ executablePath: process.env.KC_CHROME, args: ['--force-color-profile=srgb','--font-render-hinting=none'] });
for (const name of targets) {
  const p = await b.newPage({ deviceScaleFactor: 2, viewport: { width: 1440, height: 900 } });
  await p.goto('file://' + path.join(HERE, `${name}.html`), { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(350);
  const el = await p.$('body');
  await el.screenshot({ path: path.join(HERE, `${name}.png`) });
  console.log(name + '.png');
  await p.close();
}
await b.close();
