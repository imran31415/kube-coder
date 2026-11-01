/** Real socket/API browser acceptance; no production success API is mocked. */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import readline from 'node:readline';
import { chromium } from 'playwright';

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const root = path.resolve(web, '../../..');
const output = process.env.BOARD_E2E_OUTPUT ?? path.join(root, 'tmp-screenshots', `board-handoff-${Date.now()}`);
await mkdir(output, { recursive: true });
const linuxPath = value => process.platform === 'win32' ? value.replaceAll('\\', '/').replace(/^([A-Za-z]):/, (_, d) => `/mnt/${d.toLowerCase()}`) : value;
const fixture = path.resolve(web, '../tests/board_handoff_e2e_server.py');
const keeper = process.env.KEEPER_BIN ?? linuxPath(path.join(root, 'tmp-screenshots', 'kc-board-keeper'));
let processFixture;
let fixtureInfo;
const logs = [];
async function start() {
  const args = [linuxPath(fixture), '--state', linuxPath(path.join(output, 'state')), '--keeper', keeper];
  processFixture = process.platform === 'win32'
    ? spawn('wsl', ['-d', 'Ubuntu', '--', 'python3', ...args], { stdio: ['pipe', 'pipe', 'pipe'] })
    : spawn('python3', args, { stdio: ['pipe', 'pipe', 'pipe'] });
  processFixture.stderr.on('data', chunk => logs.push(chunk.toString()));
  fixtureInfo = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Fixture startup timed out: '+logs.join('').slice(-3000))), 45000);
    const lines = readline.createInterface({ input: processFixture.stdout });
    lines.on('line', line => { if (line.startsWith('E2E_READY ')) { clearTimeout(timer); resolve(JSON.parse(line.slice(10))); } });
    processFixture.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${logs.join('').slice(-3000)}`)); });
  });
}
async function stop() {
  if (!processFixture || processFixture.exitCode !== null) return;
  const done = new Promise(resolve => processFixture.once('exit', resolve));
  processFixture.stdin.end();
  await done;
}
async function until(fn, description, timeout = 20000) {
  const deadline = Date.now()+timeout;
  while (Date.now()<deadline) { const result = await fn(); if (result) return result; await new Promise(r=>setTimeout(r,100)); }
  throw new Error('Timed out: '+description);
}
let browser;
let page;
try {
  await start();
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  context.setDefaultTimeout(15000);
  async function signIn() { await context.addCookies([{ name: 'fixture-session', value: 'human', url: fixtureInfo.origin }]); }
  await signIn();
  page = await context.newPage();
  const stats = async () => (await context.request.get(fixtureInfo.origin+'/__test__/stats')).json();
  const records = async () => (await context.request.get(`${fixtureInfo.origin}/api/hypervisor/threads/${fixtureInfo.thread_id}/board-requests`)).json();
  const chatURL = () => `${fixtureInfo.origin}/hypervisor/${fixtureInfo.thread_id}`;
  await page.goto(chatURL());
  await page.getByRole('button', { name: 'Skip tour' }).click();
  const composer = page.getByRole('textbox', { name: 'Message Kube-Coder' });
  await composer.fill('Run the Board for acme/api');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await page.getByRole('button', { name: 'Approve & run', exact: true }).waitFor();
  const first = (await records()).requests[0];
  assert.equal(first.state, 'pending');
  assert.equal((await stats()).workers, 0);
  assert.equal((await stats()).vendor_reads, 0);
  await page.screenshot({ path: path.join(output, 'approval-desktop.png') });
  // Direct API bypass: shared workspace bearer and forged proxy identity fail.
  const badHeaders = { Authorization: 'Bearer '+fixtureInfo.token, 'X-Auth-Request-User': 'forged', Origin: fixtureInfo.origin };
  for (const route of ['/api/boards/e2e/runs', '/api/boards/e2e/test-fetch', '/api/boards/e2e/items/1001/actions',
      `/api/boards/run-requests/${first.id}/decision`]) {
    const result = await context.request.post(fixtureInfo.backend_url+route, { headers: badHeaders,
      data: { decision: 'approve', revision: first.revision, decision_id: 'bypass-attempt-123456' } });
    assert.equal(result.status(), 403, route);
  }
  assert.equal((await stats()).vendor_reads, 0);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.getByRole('heading', { name: 'Board request cancelled' }).waitFor();
  assert.equal((await stats()).run_count, 0);
  await composer.fill('Run the Board again for acme/api');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await page.getByRole('button', { name: 'Approve & run', exact: true }).waitFor();
  const second = (await records()).requests.find(r=>r.state==='pending');
  await page.getByRole('button', { name: 'Approve & run', exact: true }).click();
  const started = await until(async()=>{const r=(await records()).requests.find(r=>r.id===second.id);return r?.state==='started' && r;}, 'approved run');
  await until(async()=>{const r=(await records()).requests.find(r=>r.id===second.id);return r?.run?.status==='done' && r.review_needed===1;}, 'worker proposal and completion');
  assert.equal((await stats()).vendor_writes, 0, 'Propose must not write the tracker');
  assert.equal((await stats()).workers, 1);
  await page.reload();
  await page.getByRole('link', { name: 'Open Board', exact: true }).waitFor();
  await page.screenshot({ path: path.join(output, 'completed-desktop.png') });
  await page.getByRole('link', { name: 'Open Board', exact: true }).click();
  await page.waitForURL(`**/board/e2e/runs/${started.run_id}`);
  await page.getByRole('link', { name: 'Back to chat' }).waitFor();
  await page.screenshot({ path: path.join(output, 'exact-run.png') });
  await page.getByRole('link', { name: 'Back to chat' }).click();
  await page.waitForURL(`**/hypervisor/${fixtureInfo.thread_id}`);
  // A lost response is retried with the original decision ID, never a new run.
  const current=(await records()).requests.find(r=>r.id===second.id);
  const replay=await context.request.post(`${fixtureInfo.origin}/api/boards/run-requests/${second.id}/decision`, {
    headers: { Origin: fixtureInfo.origin }, data: { decision: 'approve', revision: current.revision, decision_id: current.decision_id } });
  assert.equal(replay.status(),200);
  assert.equal((await stats()).run_count,1);
  for (const theme of ['dark','light']) {
    await page.emulateMedia({ colorScheme:theme });
    await page.setViewportSize({ width:390,height:844 });
    await page.screenshot({ path:path.join(output,`completed-mobile-${theme}.png`) });
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'horizontal overflow');
  }
  const beforeRestart=await stats();
  assert.deepEqual(beforeRestart.errors,[]);
  await stop();
  await start();
  await signIn();
  await page.goto(chatURL());
  await page.getByRole('button', { name: 'Skip tour' }).click();
  await page.getByRole('link',{name:'Open Board',exact:true}).waitFor();
  assert.equal((await stats()).run_count,1,'restart must not launch another run');
  assert.equal((await stats()).workers,0,'no new worker after restart');
  await writeFile(path.join(output,'results.json'),JSON.stringify({ status:'passed', request_id:second.id,
    run_id:started.run_id, beforeRestart, afterRestart:await stats(), fixture:'stub OAuth/vendor and deterministic MCP agent; real backend/keeper/runner/review' },null,2));
  console.log(`Board handoff E2E passed. Evidence: ${output}`);
} catch (error) {
  if (page) {
    await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {});
    await writeFile(path.join(output, 'failure.txt'), String(error)+'\n'+await page.locator('body').innerText().catch(() => ''));
  }
  throw error;
} finally {
  if (browser) await browser.close();
  await stop();
  await writeFile(path.join(output,'backend.log'), logs.join(''));
}
