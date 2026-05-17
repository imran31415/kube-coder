import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await ctx.newPage();

page.on('console', (msg) => console.log(`[browser:${msg.type()}] ${msg.text()}`));
page.on('pageerror', (err) => console.log(`[pageerror] ${err.message.split('\n')[0]}`));
page.on('requestfailed', (req) => console.log(`[reqfail] ${req.url()}`));

await ctx.addInitScript(() => localStorage.setItem('kc.onboardingDone', 'true'));

console.log('=== Test 1: Click first memory row ===');
await page.goto('http://127.0.0.1:7070/memory', { waitUntil: 'networkidle' });
await page.waitForTimeout(300);
await page.locator('.mem-row').first().click();
await page.waitForTimeout(500);
console.log('md-meta:', JSON.stringify(await page.locator('.mem-detail-pane .md-meta').first().textContent()));

console.log('\n=== Test 2: Click History tab ===');
await page.locator('.md-tabs button[role="tab"]').nth(1).click();
await page.waitForTimeout(300);
console.log('history body:', JSON.stringify((await page.locator('.md-tab-body').first().textContent() ?? '').slice(0, 200)));

console.log('\n=== Test 3: Click Relations tab ===');
await page.locator('.md-tabs button[role="tab"]').nth(2).click();
await page.waitForTimeout(300);
console.log('relations body:', JSON.stringify((await page.locator('.md-tab-body').first().textContent() ?? '').slice(0, 200)));

console.log('\n=== Test 4: Click Graph (detail view tabs) ===');
await page.locator('.mem-view-tab').nth(1).click();
await page.waitForTimeout(500);
const graphVisible = await page.locator('.mem-graph svg').isVisible().catch(() => false);
console.log('Graph svg visible:', graphVisible);

console.log('\n=== Test 5: Switch back to List, then click second memory ===');
await page.locator('.mem-view-tab').nth(0).click();
await page.waitForTimeout(300);
await page.locator('.mem-row').nth(1).click();
await page.waitForTimeout(500);
console.log('md-meta (mem 2):', JSON.stringify(await page.locator('.mem-detail-pane .md-meta').first().textContent()));

console.log('\n=== Test 6: New memory form ===');
await page.locator('button:has-text("New memory")').click();
await page.waitForTimeout(300);
const drawerOpen = await page.locator('.drawer-open').isVisible().catch(() => false);
console.log('drawer opened:', drawerOpen);

await page.screenshot({ path: '/tmp/kc-debug-after.png', fullPage: false });

console.log('\n=== DONE ===');
await browser.close();
