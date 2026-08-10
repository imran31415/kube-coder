#!/usr/bin/env node
/**
 * Focused screenshots of the Board Processor (#587 / #588 / #589) — connecting
 * a board from a template, the three vendors side by side in the rail, the
 * Items tab, the Runs tab with selection strategies and the live preview, and
 * the Review tab with staged proposals plus the approval-rate strip.
 *
 * Mocks /api/boards/* entirely, so this needs only a static server for dist/
 * and no backend at all. That matters on a Windows dev box, where
 * dev_server.py cannot import server.py (it needs fcntl).
 *
 * The fixture is a plausible customer-support org rather than anyone's real
 * account: board ids and display names are exactly what `templates.fill()`
 * produces for `acme`, so what the screenshots show is what a reader following
 * the setup steps would end up with. Shapes and normalizations are the ones
 * the live runs actually returned — a `reopened` GitHub issue, a Zendesk
 * ticket at IN_PROGRESS, a Jira status nothing maps.
 *
 * ONE theme. A reader knows a dashboard can toggle; a second copy of every
 * screen says nothing the first did not. Override with SHOT_THEME=dark.
 *
 * Usage: node scripts/shoot-board.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || './board-shots');
mkdirSync(out, { recursive: true });

const CHROMIUM = chromiumPath();
// The SPA is built with base=/next/, so routes live under that prefix.
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7073/next';
const THEME = process.env.SHOT_THEME || 'light';
const now = 1_754_900_000;

const BOARDS = [
  {
    id: 'github-billing-api', vendor: 'github',
    display_name: 'GitHub Issues — acme/billing-api',
    base_url: 'https://api.github.com', credential_ref: '@workspace-github',
    credential_set: true, actions_allowed: ['comment', 'set_status'],
    created_at: now - 90000, updated_at: now - 400,
  },
  {
    id: 'jira-sup', vendor: 'jira', display_name: 'Jira Cloud — acme/SUP',
    base_url: 'https://acme.atlassian.net',
    credential_ref: '@board-creds/JIRA_API_TOKEN', credential_set: true,
    actions_allowed: ['comment', 'set_status'],
    created_at: now - 80000, updated_at: now - 300,
  },
  {
    id: 'zendesk-acme', vendor: 'zendesk', display_name: 'Zendesk Support — acme',
    base_url: 'https://acme.zendesk.com',
    credential_ref: '@board-creds/ZENDESK_OAUTH_TOKEN', credential_set: true,
    actions_allowed: ['comment', 'set_status', 'set_priority'],
    created_at: now - 6000, updated_at: now - 60,
  },
];

const F = (n, r) => ({ normalized: n, raw: r });
const P = (id, name) => (id ? { id, name } : {});

const ITEMS = {
  'github-billing-api': [
    { id: '412', key: '412', title: 'VAT applied at the wrong rate for IE customers',
      status: F('OPEN', 'open'), priority: F('HIGH', 'high'),
      assignee: P('', ''), tags: ['bug', 'billing'], updated_at: '2026-08-06T18:20:00Z' },
    // `open+reopened` is the composite that mapped to nothing until a live
    // repo turned up two of them.
    { id: '408', key: '408', title: 'Invoice PDF missing the purchase order line',
      status: F('OPEN', 'open+reopened'), priority: F('NORMAL', 'normal'),
      assignee: P('u1', 'priya'), tags: [], updated_at: '2026-08-06T17:02:00Z' },
    { id: '397', key: '397', title: 'Export CSV truncates at 1000 rows',
      status: F('CLOSED', 'closed+completed'), priority: F('LOW', 'low'),
      assignee: P('u1', 'priya'), tags: ['enhancement'], updated_at: '2026-08-05T11:00:00Z' },
    { id: '388', key: '388', title: 'Rate-limit headers missing on 429',
      status: F('OPEN', 'open+reopened'), priority: F('URGENT', 'urgent'),
      assignee: P('', ''), tags: ['bug', 'api'], updated_at: '2026-08-04T09:15:00Z' },
  ],
  'jira-sup': [
    { id: '10009', key: 'SUP-9', title: 'Rate-limit headers on 429 responses',
      status: F('IN_PROGRESS', 'In Progress'), priority: F('HIGH', 'High'),
      assignee: P('acc1', 'Priya'), tags: [], updated_at: '2026-08-07T12:00:00Z' },
    { id: '10005', key: 'SUP-5', title: 'Invoices since Jan use the wrong VAT rate',
      status: F('OPEN', 'To Do'), priority: F('URGENT', 'Highest'),
      assignee: P('', ''), tags: ['billing'], updated_at: '2026-08-07T10:30:00Z' },
    // An unmapped status passes through as the vendor wrote it rather than
    // being coerced into a bucket it does not belong in.
    { id: '10003', key: 'SUP-3', title: 'Add an audit trail to the settings page',
      status: F(null, 'Needs triage'), priority: F('NORMAL', 'Medium'),
      assignee: P('acc2', 'Sam'), tags: [], updated_at: '2026-08-06T16:45:00Z' },
  ],
  'zendesk-acme': [
    { id: '1841', key: '1841', title: 'Charged 20% VAT on an Irish billing address',
      status: F('IN_PROGRESS', 'open'), priority: F('NORMAL', 'normal'),
      assignee: P('4471', 'Priya'), tags: ['billing'],
      updated_at: '2026-08-10T16:40:00Z' },
    { id: '1838', key: '1838', title: 'Cannot download last month’s invoice',
      status: F('OPEN', 'new'), priority: F('HIGH', 'high'),
      assignee: P('', ''), tags: [], updated_at: '2026-08-10T14:05:00Z' },
    { id: '1829', key: '1829', title: 'Waiting on the customer for a PO number',
      status: F('ON_HOLD', 'pending'), priority: F('LOW', 'low'),
      assignee: P('4472', 'Sam'), tags: [], updated_at: '2026-08-09T09:12:00Z' },
  ],
};

const fullItem = (b, it) => ({
  ref: { issue_key: it.key }, body: '', contact: {}, collection: {},
  url: `${b.base_url}/browse/${it.key}`, created_at: it.updated_at,
  raw: {}, ...it,
});

const COMMENT = 'Thanks for flagging this — the rate applied does look wrong '
  + 'for an Irish billing address. I have escalated it to billing engineering '
  + 'with the invoice reference, and we will come back with a corrected '
  + 'invoice rather than asking you to re-submit anything.';

const REVIEW = {
  groups: [
    {
      disposition: 'needs_review', count: 1,
      items: [{
        board_id: 'zendesk-acme', item_id: '1841', item_key: '1841',
        item_title: 'Charged 20% VAT on an Irish billing address',
        item_url: 'https://acme.zendesk.com/agent/tickets/1841',
        content_hash: '3483e9d2735ed525', run_id: 'run-1786295532-d33e8337',
        state: 'pending', disposition: 'needs_review',
        reason: 'The customer was charged the UK 20% rate on an Irish address. '
          + 'The country-to-rate mapping treats IE as GB. I can confirm the '
          + 'symptom from the ticket, but I have no access to the invoicing '
          + 'code and cannot say whether other EU countries are affected.',
        evidence: { tool_calls: 4 },
        actions: [{
          id: 'a1', action: 'comment',
          params: { body: COMMENT, public: false },
          preview: 'PUT an internal note on ticket #1841',
          writes: 1, state: 'pending', edited: false,
        }],
        pending_actions: [{
          id: 'a1', action: 'comment',
          params: { body: COMMENT, public: false },
          preview: 'PUT an internal note on ticket #1841',
          writes: 1, state: 'pending', edited: false,
        }],
        open: true, decided_by: '', result: null,
        created_at: now - 300, updated_at: now - 120,
      }],
    },
    {
      disposition: 'needs_rescoping', count: 1,
      items: [{
        board_id: 'zendesk-acme', item_id: '1838', item_key: '1838',
        item_title: 'Cannot download last month’s invoice',
        item_url: 'https://acme.zendesk.com/agent/tickets/1838',
        content_hash: 'a91c02ee41bb', run_id: 'run-1786295532-d33e8337',
        state: 'pending', disposition: 'needs_rescoping',
        reason: 'Is this the customer portal download or the emailed copy? '
          + 'The ticket says "the invoice link" and both are links. They are '
          + 'served by different services and only one of them is ours.',
        evidence: { tool_calls: 2 },
        actions: [], pending_actions: [], open: true, decided_by: '',
        result: null, created_at: now - 900, updated_at: now - 800,
      }],
    },
  ],
  total: 2, open: 2,
};

const METRICS = {
  board_id: 'zendesk-acme',
  dispositions: { needs_review: 4, needs_rescoping: 2, completed: 3, blocked: 1 },
  decisions: { approved: 5, rejected: 1, sent_back: 2 },
  decided: 8, approved: 5, approval_rate: 0.625,
  edited_before_approval: 2, open: 2,
};

const RUNS = [{
  id: 'run-1786295532-d33e8337', board_id: 'zendesk-acme', mode: 'propose',
  status: 'done', concurrency: 3, requested_concurrency: 3, clamp_reason: '',
  created_at: now - 1200, updated_at: now - 100, finished_at: now - 100,
  error: '', listing_complete: true, truncation_reason: '',
  total: 4, counts: { pending: 0, claimed: 0, working: 0, done: 3, failed: 0, skipped: 1 },
  done: 3, failed: 0, skipped: 1,
}];

const STRATEGIES = {
  strategies: {
    'Oldest first': { order: 'updated_at asc', limit: 25 },
    'Urgent only': { priority: ['URGENT', 'HIGH'], order: 'priority', limit: 10 },
    Unassigned: { unassigned: true, order: 'updated_at asc', limit: 25 },
  },
  builtins: ['Oldest first', 'Urgent only', 'Unassigned'],
};

const PREVIEW = {
  select: { priority: ['URGENT', 'HIGH'], order: 'priority', limit: 10 },
  matched: 12, would_work: 7, skipped_already_processed: 5,
  held_by_another_run: 0, listing_complete: true, truncation_reason: '',
  sample: [
    { id: '388', key: '388', title: 'Rate-limit headers missing on 429', status: 'OPEN', priority: 'URGENT' },
    { id: '412', key: '412', title: 'VAT applied at the wrong rate for IE customers', status: 'OPEN', priority: 'HIGH' },
  ],
};

// Only ever a name, a format and a last-four hint. There is deliberately no
// endpoint that reads a value back, so there is nothing here to redact.
const CREDENTIALS = [
  { name: 'JIRA_API_TOKEN', format: 'basic', username: 'ops@acme.example',
    hint: '…9c1d', created_at: now - 80000, updated_at: now - 80000 },
  { name: 'ZENDESK_OAUTH_TOKEN', format: 'token', username: '',
    hint: '…a7e3', created_at: now - 6000, updated_at: now - 6000 },
];

// Exactly what boards/templates.py:listing() returns, so the connect dialog
// renders the real fields rather than an invented set.
const TEMPLATES = [
  {
    id: 'github-issues', display_name: 'GitHub Issues', vendor: 'github',
    actions: ['comment', 'set_status'],
    needs: ['Replace OWNER and REPO with your repository.',
            'Credential: none to paste — the workspace brokers its own GitHub '
            + 'App token.'],
    placeholders: [
      { token: 'OWNER', label: 'Owner', example: 'acme',
        help: 'The user or organisation the repository belongs to.' },
      { token: 'REPO', label: 'Repository', example: 'billing-api',
        help: 'The repository name on its own, without the owner.' },
    ],
    credential: null, verified: false,
    note: 'a starting point, not a verified connector — run test-fetch before trusting it',
  },
  {
    id: 'jira-cloud', display_name: 'Jira Cloud', vendor: 'jira',
    actions: ['comment', 'set_status'],
    needs: ['Replace YOURSITE with your Atlassian site.',
            'Replace PROJ in the JQL with your project key.'],
    placeholders: [
      { token: 'YOURSITE', label: 'Atlassian site', example: 'acme',
        help: 'The label only — "acme" for acme.atlassian.net.' },
      { token: 'PROJ', label: 'Project key', example: 'SUP',
        help: 'The key issues are prefixed with, e.g. SUP-142 → SUP.' },
    ],
    credential: {
      name: 'JIRA_API_TOKEN', format: 'basic',
      username_label: 'Atlassian account email', secret_label: 'API token',
      help: 'id.atlassian.com → Security → API tokens → Create. Paste the RAW '
        + 'token; the server composes the Basic header.',
    },
    verified: false,
    note: 'a starting point, not a verified connector — run test-fetch before trusting it',
  },
  {
    id: 'zendesk', display_name: 'Zendesk Support', vendor: 'zendesk',
    actions: ['comment', 'set_priority', 'set_status'],
    // Verbatim from boards/templates.py NEEDS['zendesk'] — the PKCE steps are
    // the whole reason this panel exists, so an abridged version here would be
    // showing a screen the product does not have.
    needs: [
      'Replace YOURSUBDOMAIN with your Zendesk subdomain (the label only: '
      + '"acme" for acme.zendesk.com).',
      'Credential ZENDESK_OAUTH_TOKEN: format "token", value = an OAuth ACCESS '
      + 'TOKEN. API tokens do NOT work — Zendesk withdrew them from the admin '
      + 'UI and retires the existing ones on 2027-04-30.',
      'Mint one with the authorization-code + PKCE flow: Admin Center → Apps '
      + 'and integrations → APIs → OAuth clients → add a client, set any '
      + 'redirect URL you control, and note its Unique identifier. Then run '
      + '`python3 boards/zendesk_oauth.py start <subdomain> <identifier> '
      + '<redirect-url>`, approve in the browser, and feed the `code` from the '
      + 'redirect back with `zendesk_oauth.py finish <code>`.',
      'Do NOT use the implicit grant (response_type=token). Zendesk OAuth '
      + 'clients are Public or Confidential, and a Public client — what the '
      + 'admin UI creates — is only permitted the code+PKCE flow. The implicit '
      + 'URL simply refuses, which reads like a misconfigured client.',
      'Check the comment action\'s "public" parameter before approving — a '
      + 'public comment is visible to the customer.',
    ],
    placeholders: [
      { token: 'YOURSUBDOMAIN', label: 'Zendesk subdomain', example: 'acme',
        help: 'The label only — "acme" for acme.zendesk.com.' },
    ],
    credential: {
      name: 'ZENDESK_OAUTH_TOKEN', format: 'token',
      username_label: '', secret_label: 'OAuth access token',
      help: 'An OAuth access token, not an API token — see the setup steps.',
    },
    verified: false,
    note: 'a starting point, not a verified connector — run test-fetch before trusting it',
  },
];

/** The board the connect flow creates, once it has been created. Lets the
 *  rail behind the dialog agree with the result panel in front of it. */
const CREATED = BOARDS[2];

async function mockBoards(page, boardId, { empty = false, failFetch = false } = {}) {
  const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  let created = false;
  const list = () => (empty ? (created ? [CREATED] : []) : BOARDS);

  await page.route('**/api/mode', (r) => json(r, {
    readOnly: false, authed: true, authMode: 'basic', ctoEnabled: true, boardEnabled: true,
  }));
  await page.route('**/api/events', (r) =>
    r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }));

  // Order matters: Playwright matches the LAST registered route first, so the
  // specific patterns below are registered after the generic listing.
  await page.route(/\/api\/boards$/, (r) => {
    if (r.request().method() === 'POST') {       // create
      created = true;
      return json(r, CREATED);
    }
    return json(r, { boards: list() });
  });
  await page.route(/\/api\/boards\/templates$/, (r) => json(r, { templates: TEMPLATES }));
  await page.route(/\/api\/boards\/credentials$/, (r) =>
    json(r, { credentials: CREDENTIALS }));

  for (const b of BOARDS) {
    const items = (ITEMS[b.id] || []).map((it) => fullItem(b, it));
    await page.route(new RegExp(`/api/boards/${b.id}$`), (r) => json(r, b));
    await page.route(new RegExp(`/api/boards/${b.id}/items`), (r) => json(r, {
      items, complete: true, truncation_reason: '', pages_fetched: 1,
    }));
    await page.route(new RegExp(`/api/boards/${b.id}/runs$`), (r) => json(r, { runs: RUNS }));
    await page.route(new RegExp(`/api/boards/${b.id}/review`), (r) => json(r, REVIEW));
    await page.route(new RegExp(`/api/boards/${b.id}/metrics`), (r) => json(r, METRICS));
    await page.route(new RegExp(`/api/boards/${b.id}/strategies$`), (r) => json(r, STRATEGIES));
    await page.route(new RegExp(`/api/boards/${b.id}/strategies/preview`), (r) => json(r, PREVIEW));
  }

  // The write half of the connect flow, registered last so these win over the
  // read patterns above. Credential first, then fill, then create (handled by
  // the /api/boards route), then the fetch that decides whether any of it
  // worked — the same order the store performs them in.
  await page.route(/\/api\/boards\/credentials\/[A-Z0-9_]+$/, (r) =>
    json(r, { credentials: CREDENTIALS }));
  await page.route(/\/api\/boards\/templates\/[a-z-]+\/fill$/, (r) =>
    json(r, { connector: { id: CREATED.id, vendor: CREATED.vendor }, verified: false }));
  await page.route(new RegExp(`/api/boards/${CREATED.id}/test-fetch$`), (r) =>
    (failFetch
      ? r.fulfill({
          status: 502,
          contentType: 'application/json',
          // The shape a real failure takes: the vendor answered, and it said
          // no. Which is why the board is kept rather than rolled back.
          body: JSON.stringify({
            error: 'HTTP 401 from https://acme.zendesk.com/api/v2/tickets.json'
              + ' — the credential ZENDESK_OAUTH_TOKEN was rejected',
          }),
        })
      : json(r, {
          items: (ITEMS[CREATED.id] || []).map((it) => fullItem(CREATED, it)),
          complete: true, truncation_reason: '', pages_fetched: 1,
          raw_count: 3, map_errors: [], truncated_for_display: false,
        })));

  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
  await page.addInitScript(() => localStorage.setItem('kc.onboardingDone', 'true'));
  if (boardId) {
    await page.addInitScript((id) => localStorage.setItem('kc.board.selected', id), boardId);
  }
}

const WIDE = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };

/**
 * The connect flow, screen by screen — what a new user actually walks through.
 *
 * Every one of these starts from an EMPTY workspace, because that is the state
 * the flow exists for and the state the old empty rail answered with "ask an
 * agent to build one for you". The last two are the two endings: a board that
 * fetched, and a board that was created and could not fetch.
 */
const CONNECT_SHOTS = [
  { name: 'board-connect-empty' },
  { name: 'board-connect-picker', open: true },
  // GitHub asks for no secret at all — the workspace brokers its own App
  // token — and saying so is better than an empty box the user hunts for.
  { name: 'board-connect-github', open: true, pick: 'GitHub Issues',
    fill: { acme: 'acme', 'billing-api': 'billing-api' } },
  // Jira composes Basic, so the form asks for the username half too.
  { name: 'board-connect-jira', open: true, pick: 'Jira Cloud',
    fill: { acme: 'acme', SUP: 'SUP', 'you@example.com': 'ops@acme.example',
            'paste the raw token': 'ATATT3xFfGF0T4Kx9pQ' } },
  // Taller, because the Zendesk steps are five paragraphs and the point of the
  // shot is that all five are there.
  { name: 'board-connect-steps', open: true, pick: 'Zendesk Support', steps: true,
    viewport: { width: 1440, height: 1180 } },
  { name: 'board-connect', open: true, pick: 'Zendesk Support',
    fill: { acme: 'acme', 'paste the raw token': 'gAAAAABm2Qk7cZ1xR4' } },
  { name: 'board-connect-verified', open: true, pick: 'Zendesk Support',
    fill: { acme: 'acme', 'paste the raw token': 'gAAAAABm2Qk7cZ1xR4' },
    submit: true },
  { name: 'board-connect-failed', open: true, pick: 'Zendesk Support',
    fill: { acme: 'acme', 'paste the raw token': 'gAAAAABm2Qk7cZ1xR4' },
    submit: true, failFetch: true },
  { name: 'board-connect-mobile', open: true, pick: 'Zendesk Support',
    fill: { acme: 'acme', 'paste the raw token': 'gAAAAABm2Qk7cZ1xR4' },
    viewport: PHONE },
];

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  for (const s of CONNECT_SHOTS) {
    const ctx = await browser.newContext({
      viewport: s.viewport || WIDE, deviceScaleFactor: 2, colorScheme: THEME,
    });
    const page = await ctx.newPage();
    await mockBoards(page, null, { empty: true, failFetch: !!s.failFetch });
    await page.goto(`${BASE}/board`, { waitUntil: 'load' });
    await page.waitForSelector('.route-board', { timeout: 20000 });
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), THEME);
    await page.waitForTimeout(400);

    if (s.open) {
      await page.getByRole('button', { name: /connect a board/i }).first().click();
      await page.waitForTimeout(400);
    }
    if (s.pick) {
      await page.getByRole('radio', { name: new RegExp(s.pick, 'i') }).first().click();
      await page.waitForTimeout(300);
    }
    for (const [placeholder, value] of Object.entries(s.fill || {})) {
      await page.getByPlaceholder(placeholder, { exact: true }).first().fill(value);
    }
    if (s.steps) {
      await page.locator('.board-connect-steps summary').first().click();
      await page.waitForTimeout(300);
    }
    if (s.submit) {
      await page.getByRole('button', { name: /connect and fetch/i }).click();
      await page.waitForTimeout(900);
    }
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${out}/${s.name}.png` });
    await ctx.close();
    console.log(`${s.name}.png`);
  }

  const shots = [
    { name: 'board-items', tab: 'Items', board: 'github-billing-api', viewport: WIDE },
    { name: 'board-runs', tab: 'Runs', board: 'zendesk-acme', viewport: WIDE },
    { name: 'board-review', tab: 'Review', board: 'zendesk-acme', viewport: WIDE },
    { name: 'board-credentials', tab: 'Credentials', board: 'zendesk-acme', viewport: WIDE },
    { name: 'board-review-mobile', tab: 'Review', board: 'zendesk-acme',
      viewport: PHONE },
    // The preview line only exists after asking for it — "this would work 7,
    // skip 5 already processed" is the whole point of the button.
    { name: 'board-preview', tab: 'Runs', board: 'github-billing-api', preview: true,
      viewport: WIDE },
  ];
  for (const s of shots) {
    const ctx = await browser.newContext({
      viewport: s.viewport, deviceScaleFactor: 2, colorScheme: THEME,
    });
    const page = await ctx.newPage();
    await mockBoards(page, s.board);
    await page.goto(`${BASE}/board?board=${s.board}`, { waitUntil: 'load' });
    await page.waitForSelector('.route-board', { timeout: 20000 });
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), THEME);
    await page.waitForTimeout(500);
    if (s.tab && s.tab !== 'Items') {
      const tab = page.locator('.board-tab', { hasText: s.tab }).first();
      if (await tab.count()) { await tab.click(); await page.waitForTimeout(700); }
    }
    if (s.preview) {
      const btn = page.getByRole('button', { name: /what would this work/i }).first();
      if (await btn.count()) { await btn.click(); await page.waitForTimeout(900); }
    }
    await page.waitForTimeout(400);
    const file = `${s.name}.png`;
    await page.screenshot({ path: `${out}/${file}` });
    await ctx.close();
    console.log(file);
  }
} finally {
  await browser.close();
}
