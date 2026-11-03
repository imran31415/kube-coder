import { useEffect, useState } from 'preact/hooks';
import {
  triggers,
  filteredTriggers,
  triggerFilter,
  triggersError,
  refreshTriggers,
  startTriggerPolling,
  stopTriggerPolling,
  fire,
  toggleSuspend,
  removeTrigger,
} from '../../store/triggers';
import { drawerOpen, type DrawerKey } from '../../store/ui';
import { useIsMobile } from '../../hooks/useMediaQuery';
import {
  createCron,
  createWebhook,
  createPageWatch,
  type Trigger,
  type TriggerAction,
  type TriggerBoardRun,
  type TriggerKind,
  type WebhookMatch,
} from '../../api/triggers';
import { listBoards, type Board } from '../../api/boards';
import { Button } from '../../components/primitives/Button';
import { MutatorOnly } from '../../components/MutatorOnly';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { EmptyState } from '../../components/primitives/EmptyState';
import { Drawer } from '../../components/Drawer';
import { BottomSheet } from '../../components/BottomSheet';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { TriggerRuns } from './TriggerRuns';
import { pushToast } from '../../store/ui';
import './triggers.css';

export function TriggersRoute() {
  const isMobile = useIsMobile();

  useEffect(() => {
    startTriggerPolling(30000);
    return () => stopTriggerPolling();
  }, []);

  const list = filteredTriggers.value;

  return (
    <div class="route route-triggers">
      <header class="route-header route-header-with-action">
        <div>
          <h1 class="route-title">Triggers</h1>
          <p class="route-subtitle muted">
            Webhooks, crons and page watches in one list. {triggers.value.length} trigger{triggers.value.length === 1 ? '' : 's'}.
          </p>
        </div>
        <MutatorOnly>
          <Button variant="primary" size="md" onClick={() => (drawerOpen.value = 'trigger-edit' as DrawerKey)}>
            <Icon name="plus" size={14} /> New trigger
          </Button>
        </MutatorOnly>
      </header>

      <div class="trig-toolbar">
        <Input
          fullWidth
          placeholder="Filter by id, prompt, schedule, or URL…"
          value={triggerFilter.value}
          onInput={(e) => (triggerFilter.value = (e.target as HTMLInputElement).value)}
          aria-label="Filter triggers"
        />
      </div>

      {triggersError.value && <div class="trig-error" role="alert">{triggersError.value}</div>}

      {list.length === 0 ? (
        <EmptyState
          icon={<Icon name="triggers" size={24} />}
          title={triggerFilter.value ? 'No matches' : 'No triggers yet'}
          description={
            triggerFilter.value
              ? 'Try clearing the filter.'
              : 'Triggers fire Claude tasks automatically — on a schedule, via webhook, when a watched page changes, or manually.'
          }
          action={
            !triggerFilter.value && (
              <MutatorOnly>
                <Button variant="primary" onClick={() => (drawerOpen.value = 'trigger-edit' as DrawerKey)}>
                  <Icon name="plus" size={14} /> Create trigger
                </Button>
              </MutatorOnly>
            )
          }
        />
      ) : (
        <ul class="trig-list" role="list">
          {list.map((t) => (
            <li key={`${t.kind}:${t.id}`}>
              <TriggerRow t={t} />
            </li>
          ))}
        </ul>
      )}

      {!isMobile ? (
        <Drawer
          open={drawerOpen.value === ('trigger-edit' as DrawerKey)}
          onClose={() => (drawerOpen.value = null)}
          title="New trigger"
          width={560}
        >
          <TriggerForm onClose={() => (drawerOpen.value = null)} />
        </Drawer>
      ) : (
        <BottomSheet
          open={drawerOpen.value === ('trigger-edit' as DrawerKey)}
          onClose={() => (drawerOpen.value = null)}
          initialSnap="full"
          title="New trigger"
        >
          <TriggerForm onClose={() => (drawerOpen.value = null)} />
        </BottomSheet>
      )}
    </div>
  );
}

/** Short "5m ago" gloss. Local because the dashboard has no shared relative-time
 *  util yet — TaskList carries its own copy of the same idea. */
function since(ts?: number | null): string {
  if (!ts) return 'never';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

/** Per-kind presentation. A table rather than nested ternaries: with three
 *  kinds a `cron ? … : …` silently routes anything new down the webhook
 *  branch, which is how a new kind ends up mislabelled and unpausable. */
const KIND_META: Record<
  TriggerKind,
  { label: string; tone: 'accent' | 'info' | 'success'; pausable: boolean; deleteBody: string }
> = {
  cron: {
    label: 'cron',
    tone: 'accent',
    pausable: true,
    deleteBody: 'The CronJob and its in-cluster Secret will be deleted.',
  },
  webhook: {
    label: 'webhook',
    tone: 'info',
    pausable: false,
    deleteBody: 'The webhook config will be deleted and any external sender will start getting 404s.',
  },
  'page-watch': {
    label: 'page watch',
    tone: 'success',
    pausable: true,
    deleteBody: 'The watch, its CronJob and its in-cluster Secret will be deleted. The page itself is untouched.',
  },
};

function TriggerRow({ t }: { t: Trigger }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  // Collapsed by default, and mounted only while open: the ledger is a
  // per-trigger fetch, and opening the Triggers tab must not turn into one
  // request per row (#91).
  const [showRuns, setShowRuns] = useState(false);
  // A webhook that filters deliveries can only be tested with an event and a
  // payload that match, so its Fire button opens a small form instead (#709).
  const [showTest, setShowTest] = useState(false);
  const needsTestForm = t.kind === 'webhook' && (!!t.match || t.action === 'board_run');
  const meta = KIND_META[t.kind] ?? KIND_META.webhook;
  const tone = t.suspended ? 'warn' : meta.tone;
  const label = t.suspended ? `${meta.label} · paused` : meta.label;
  return (
    <article class="trig-row">
      <div class="trig-row-head">
        <Pill tone={tone} mono>{label}</Pill>
        <span class="trig-row-id mono">{t.id}</span>
        {t.schedule && <span class="trig-row-sched mono">{t.schedule}</span>}
        <div class="trig-row-actions">
          {/* Outside MutatorOnly: reading a trigger's history is not a
              mutation, and the read-only demo already lists the triggers. */}
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={showRuns}
            onClick={() => setShowRuns((v) => !v)}
          >
            {showRuns ? 'Hide runs' : 'Runs'}
          </Button>
          <MutatorOnly>
            {/* A paused page-watch refuses the check server-side, so the
                button says so up front rather than handing back a 409. */}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => (needsTestForm ? setShowTest((v) => !v) : fire(t))}
              aria-expanded={needsTestForm ? showTest : undefined}
              disabled={t.kind === 'page-watch' && t.suspended}
              title={
                t.kind === 'page-watch' && t.suspended
                  ? 'This watch is paused - resume it to check now'
                  : undefined
              }
            >
              <Icon name="play" size={12} />{' '}
              {t.kind === 'page-watch' ? 'Check now' : needsTestForm ? 'Test' : 'Fire now'}
            </Button>
            {meta.pausable && (
              <Button size="sm" variant="ghost" onClick={() => toggleSuspend(t)}>
                {t.suspended ? 'Resume' : 'Pause'}
              </Button>
            )}
            <Button
              size="sm"
              variant="danger"
              onClick={() => setConfirmDelete(true)}
            >
              Delete
            </Button>
            <ConfirmDialog
              open={confirmDelete}
              title={`Delete ${t.id}?`}
              body={meta.deleteBody}
              confirmLabel="Delete"
              destructive
              onConfirm={() => {
                setConfirmDelete(false);
                void removeTrigger(t);
              }}
              onCancel={() => setConfirmDelete(false)}
            />
          </MutatorOnly>
        </div>
      </div>
      {t.kind === 'page-watch' && (
        <div class="trig-row-watch">
          <span class="trig-row-url mono" title={t.url}>{t.url}</span>
          {t.selector && <span class="trig-row-sched mono">{t.selector}</span>}
        </div>
      )}
      {t.kind === 'page-watch' && (
        <div class="trig-row-status">
          {t.last_error
            ? <Pill tone="danger">check failed</Pill>
            : !t.last_hash
              ? <Pill tone="neutral">waiting for first check</Pill>
              : <Pill tone="neutral" mono>{`checked ${since(t.last_checked_at)}`}</Pill>}
          {!t.last_error && t.last_changed_at && (
            <span class="muted mono">changed {since(t.last_changed_at)}</span>
          )}
          {t.last_error && <span class="trig-row-err muted">{t.last_error}</span>}
        </div>
      )}
      {t.action === 'board_run' && t.board_run ? (
        <p class="trig-row-prompt muted">{describeBoardRun(t.board_run)}</p>
      ) : (
        <p class="trig-row-prompt muted">{t.prompt}</p>
      )}
      {t.kind === 'webhook' && (t.match || t.item_id_from) && (
        <p class="trig-row-meta muted mono">{describeMatch(t.match, t.item_id_from)}</p>
      )}
      {/* A Board run has no working directory of its own: the board's
          agents decide where they work. */}
      {(() => {
        const parts = [t.action === 'board_run' ? '' : t.workdir, t.timezone].filter(Boolean);
        return parts.length > 0 && (
          <div class="trig-row-meta muted mono">{parts.join(' · ')}</div>
        );
      })()}
      {showTest && <WebhookTestForm t={t} onDone={() => setShowTest(false)} />}
      {showRuns && <TriggerRuns t={t} />}
    </article>
  );
}

/** "Board run on acme-issues · tags ai · up to 5 · 2 at once · propose". */
export function describeBoardRun(br: TriggerBoardRun): string {
  const parts = [`Board run on ${br.board_id}`];
  const tags = br.select?.tags ?? [];
  if (tags.length) parts.push(`tags ${tags.join(', ')}`);
  if (br.select?.limit) parts.push(`up to ${br.select.limit}`);
  if ((br.concurrency ?? 1) > 1) parts.push(`${br.concurrency} at once`);
  parts.push('every write waits for your approval');
  return parts.join(' · ');
}

export function describeMatch(match?: WebhookMatch, itemIdFrom?: string): string {
  const parts: string[] = [];
  if (match?.event) parts.push(`event ${match.event}`);
  for (const [path, want] of Object.entries(match?.payload ?? {})) parts.push(`${path} = ${want}`);
  if (itemIdFrom) parts.push(`item from ${itemIdFrom}`);
  return parts.join(' · ');
}

/** `label.name = ai` lines → `{ "label.name": "ai" }`. Blank lines are
 *  ignored; a line without `=` is an error rather than a filter that quietly
 *  matches nothing. */
export function parseMatchPayload(text: string): { payload: Record<string, string>; error: string } {
  const payload: Record<string, string> = {};
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.indexOf('=');
    if (at <= 0) return { payload, error: `"${line}" is not path = value` };
    payload[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return { payload, error: '' };
}

/** A payload that satisfies the webhook's own filter: the starting point for
 *  a Test, so the form does not open on a delivery it would skip. */
export function samplePayload(t: Pick<Trigger, 'match' | 'item_id_from'>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  const put = (path: string, value: unknown) => {
    const keys = path.split('.');
    let cur = out as Record<string, unknown>;
    keys.slice(0, -1).forEach((k) => {
      if (typeof cur[k] !== 'object' || cur[k] === null) cur[k] = {};
      cur = cur[k] as Record<string, unknown>;
    });
    cur[keys[keys.length - 1]] = value;
  };
  for (const [path, want] of Object.entries(t.match?.payload ?? {})) put(path, want);
  if (t.item_id_from) put(t.item_id_from, 'REPLACE-with-an-item-id');
  return out;
}

function WebhookTestForm({ t, onDone }: { t: Trigger; onDone: () => void }) {
  const [event, setEvent] = useState(t.match?.event ?? '');
  const [body, setBody] = useState(() => JSON.stringify(samplePayload(t), null, 2));
  const [busy, setBusy] = useState(false);
  let parsed: unknown = null;
  let parseError = '';
  try {
    parsed = JSON.parse(body || '{}');
  } catch {
    parseError = 'Not valid JSON';
  }
  return (
    <form
      class="tf trig-test"
      aria-label={`Test ${t.id}`}
      onSubmit={async (e) => {
        e.preventDefault();
        if (parseError) return;
        setBusy(true);
        try {
          await fire(t, { event: event.trim(), payload: parsed });
          onDone();
        } finally {
          setBusy(false);
        }
      }}
    >
      <label class="tf-field">
        <span class="tf-label">Event</span>
        <Input
          fullWidth
          value={event}
          placeholder="issues"
          onInput={(e) => setEvent((e.target as HTMLInputElement).value)}
        />
        <span class="tf-hint muted">Stands in for the X-GitHub-Event header a real delivery sends.</span>
      </label>
      <label class="tf-field">
        <span class="tf-label">Payload</span>
        <textarea
          class="tf-textarea mono"
          rows={6}
          value={body}
          onInput={(e) => setBody((e.target as HTMLTextAreaElement).value)}
        />
        {parseError && <span class="tf-error">{parseError}</span>}
      </label>
      <div class="tf-actions">
        <Button variant="ghost" type="button" onClick={onDone}>Cancel</Button>
        <Button variant="primary" type="submit" disabled={!!parseError || busy}>
          <Icon name="play" size={12} /> Send test
        </Button>
      </div>
    </form>
  );
}

function TriggerForm({ onClose }: { onClose: () => void }) {
  const [kind, setKind] = useState<TriggerKind>('cron');
  const [id, setId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [schedule, setSchedule] = useState('0 * * * *');
  const [timezone, setTimezone] = useState('UTC');
  const [workdir, setWorkdir] = useState('/home/dev');
  const [url, setUrl] = useState('');
  const [selector, setSelector] = useState('');
  const [includeContent, setIncludeContent] = useState(false);
  const [busy, setBusy] = useState(false);
  // What happens on fire (#709 part 2). Page watches only start Builds.
  const [action, setAction] = useState<TriggerAction>('build');
  const [boardList, setBoardList] = useState<Board[] | null>(null);
  const [boardsFailed, setBoardsFailed] = useState('');
  const [boardId, setBoardId] = useState('');
  const [tags, setTags] = useState('');
  const [limit, setLimit] = useState('5');
  const [concurrency, setConcurrency] = useState('1');
  const [matchEvent, setMatchEvent] = useState('');
  const [matchPayload, setMatchPayload] = useState('');
  const [itemIdFrom, setItemIdFrom] = useState('');
  // A new webhook's secret is shown once, here, rather than in a toast that
  // truncates it — a Board-run webhook is useless until GitHub has it.
  const [created, setCreated] = useState<{ url: string; secret: string } | null>(null);

  const boardRun = action === 'board_run' && kind !== 'page-watch';
  useEffect(() => {
    if (!boardRun || boardList) return;
    listBoards()
      .then((r) => {
        setBoardList(r.boards);
        setBoardId((cur) => cur || r.boards[0]?.id || '');
      })
      .catch((err) => setBoardsFailed(err instanceof Error ? err.message : 'Could not list boards'));
  }, [boardRun, boardList]);

  const idOk = /^[a-z0-9-]+$/.test(id);
  const scheduleOk = schedule.trim().split(/\s+/).length === 5 || /^@\w+$/.test(schedule.trim());
  // http(s) only, mirroring PageWatchManager.validate_url. The server is still
  // the authority — this only saves the user a round-trip.
  const urlOk = /^https?:\/\/[^\s/$.?#].[^\s]*$/i.test(url.trim());
  // Explicitly per-kind. The old `kind === 'webhook' || scheduleOk` shape would
  // have quietly held a page-watch to the cron rule and nothing else.
  const kindValid =
    kind === 'webhook' ? true
      : kind === 'cron' ? scheduleOk
        : scheduleOk && urlOk;
  const limitN = Number(limit);
  const concurrencyN = Number(concurrency);
  const limitOk = Number.isInteger(limitN) && limitN >= 1 && limitN <= 500;
  const concurrencyOk = Number.isInteger(concurrencyN) && concurrencyN >= 1 && concurrencyN <= 8;
  const match = parseMatchPayload(matchPayload);
  const boardRunValid = !!boardId && limitOk && concurrencyOk;
  const webhookValid = kind !== 'webhook' || !match.error;
  const valid =
    idOk && kindValid && webhookValid && (boardRun ? boardRunValid : prompt.trim().length > 0);

  function boardRunBody(): TriggerBoardRun {
    const tagList = tags.split(',').map((x) => x.trim()).filter(Boolean);
    return {
      board_id: boardId,
      select: { ...(tagList.length ? { tags: tagList } : {}), limit: limitN },
      concurrency: concurrencyN,
    };
  }

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    try {
      if (kind === 'cron') {
        await createCron(
          boardRun
            ? { id, schedule, timezone, action: 'board_run', board_run: boardRunBody() }
            : { id, schedule, prompt_template: prompt, workdir, timezone },
        );
        pushToast('Cron created', { kind: 'success' });
      } else if (kind === 'page-watch') {
        const w = await createPageWatch({
          id, url: url.trim(), schedule, prompt_template: prompt, workdir, timezone,
          selector: selector.trim() || undefined,
          include_content: includeContent,
        });
        if (w.warning) {
          // Saved, but with no CronJob it will never check. Never silent.
          pushToast(`Saved, but the schedule did not apply: ${w.warning}`, { kind: 'warn', ttl: 12000 });
        } else if (w.redirected_from) {
          pushToast(`Watching ${w.url} (followed a redirect)`, { kind: 'success', ttl: 10000 });
        } else {
          pushToast('Page watch created. The first check records a baseline.', { kind: 'success' });
        }
      } else {
        const filter: WebhookMatch = {
          ...(matchEvent.trim() ? { event: matchEvent.trim() } : {}),
          ...(Object.keys(match.payload).length ? { payload: match.payload } : {}),
        };
        const w = await createWebhook({
          id,
          ...(boardRun
            ? { action: 'board_run' as const, board_run: boardRunBody(),
                ...(itemIdFrom.trim() ? { item_id_from: itemIdFrom.trim() } : {}) }
            : { prompt_template: prompt, workdir }),
          ...(Object.keys(filter).length ? { match: filter } : {}),
        });
        await refreshTriggers();
        if (w.hmac_secret_once) {
          setCreated({ url: w.receive_url ?? `/api/webhooks/${id}`, secret: w.hmac_secret_once });
          return;
        }
        pushToast('Webhook created', { kind: 'success' });
      }
      await refreshTriggers();
      onClose();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Create failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
  }

  if (created) {
    return (
      <div class="tf" role="status">
        <p>
          Webhook <span class="mono">{id}</span> created. Copy the secret now: it is not
          shown again.
        </p>
        <label class="tf-field">
          <span class="tf-label">Payload URL</span>
          <Input fullWidth readOnly value={created.url} onFocus={(e) => (e.target as HTMLInputElement).select()} />
        </label>
        <label class="tf-field">
          <span class="tf-label">Secret</span>
          <Input fullWidth readOnly class="mono" value={created.secret} onFocus={(e) => (e.target as HTMLInputElement).select()} />
          <span class="tf-hint muted">
            In GitHub: Settings → Webhooks → Add webhook, content type application/json.
          </span>
        </label>
        <div class="tf-actions">
          <Button variant="primary" type="button" onClick={onClose}>Done</Button>
        </div>
      </div>
    );
  }

  return (
    <form class="tf" onSubmit={onSubmit}>
      <div class="tf-kind-row">
        <span class="tf-label">Kind</span>
        <div class="seg" role="group" aria-label="Trigger kind">
          {(['cron', 'webhook', 'page-watch'] as TriggerKind[]).map((k) => (
            <button
              key={k}
              type="button"
              class={`seg-item ${kind === k ? 'seg-item-active' : ''}`}
              aria-pressed={kind === k}
              onClick={() => {
                // A watch polls; a cron reports. Carry the sensible default
                // across the switch, but never clobber a schedule the user
                // has already typed. Mirrors emptyDraft() on mobile.
                setSchedule((cur) =>
                  cur === '0 * * * *' && k === 'page-watch' ? '*/5 * * * *'
                    : cur === '*/5 * * * *' && k !== 'page-watch' ? '0 * * * *'
                      : cur,
                );
                setKind(k);
              }}
            >
              {k}
            </button>
          ))}
        </div>
      </div>

      <label class="tf-field">
        <span class="tf-label">ID</span>
        <Input
          fullWidth
          value={id}
          placeholder="hourly-summary"
          onInput={(e) => setId((e.target as HTMLInputElement).value)}
          required
        />
        {id && !idOk && <span class="tf-error">Lowercase letters, digits, and hyphens only.</span>}
      </label>

      {kind !== 'page-watch' && (
        <div class="tf-kind-row">
          <span class="tf-label">What should happen?</span>
          <div class="seg" role="group" aria-label="What should happen?">
            {([['build', 'Start a Build'], ['board_run', 'Start a Board run']] as [TriggerAction, string][]).map(
              ([a, label]) => (
                <button
                  key={a}
                  type="button"
                  class={`seg-item ${action === a ? 'seg-item-active' : ''}`}
                  aria-pressed={action === a}
                  onClick={() => setAction(a)}
                >
                  {label}
                </button>
              ),
            )}
          </div>
        </div>
      )}

      {boardRun ? (
        <>
          <label class="tf-field">
            <span class="tf-label">Board</span>
            {boardList && boardList.length > 0 ? (
              <select
                class="tf-select"
                value={boardId}
                onChange={(e) => setBoardId((e.target as HTMLSelectElement).value)}
              >
                {boardList.map((b) => (
                  <option key={b.id} value={b.id}>{b.display_name || b.id}</option>
                ))}
              </select>
            ) : (
              <span class="tf-hint muted">
                {boardsFailed || (boardList ? 'No boards yet. Connect one on the Board page first.' : 'Loading boards…')}
              </span>
            )}
          </label>
          <div class="tf-row tf-row-3">
            <label class="tf-field">
              <span class="tf-label">Tags <span class="muted">(optional)</span></span>
              <Input
                fullWidth
                value={tags}
                placeholder="ai, triage"
                onInput={(e) => setTags((e.target as HTMLInputElement).value)}
              />
            </label>
            <label class="tf-field">
              <span class="tf-label">Limit</span>
              <Input
                fullWidth
                type="number"
                min={1}
                max={500}
                value={limit}
                onInput={(e) => setLimit((e.target as HTMLInputElement).value)}
              />
              {!limitOk && <span class="tf-error">1 to 500</span>}
            </label>
            <label class="tf-field">
              <span class="tf-label">At once</span>
              <Input
                fullWidth
                type="number"
                min={1}
                max={8}
                value={concurrency}
                onInput={(e) => setConcurrency((e.target as HTMLInputElement).value)}
              />
              {!concurrencyOk && <span class="tf-error">1 to 8</span>}
            </label>
          </div>
          <p class="tf-fixed">
            <Pill tone="neutral">propose</Pill> Every write waits for your approval.
          </p>
        </>
      ) : (
        <label class="tf-field">
          <span class="tf-label">Prompt template</span>
          <textarea
            class="tf-textarea"
            rows={5}
            required
            placeholder="What should Claude do when this trigger fires?"
            value={prompt}
            onInput={(e) => setPrompt((e.target as HTMLTextAreaElement).value)}
          />
        </label>
      )}

      {kind === 'webhook' && (
        <>
          <div class="tf-row">
            <label class="tf-field">
              <span class="tf-label">Only for event <span class="muted">(optional)</span></span>
              <Input
                fullWidth
                value={matchEvent}
                placeholder="issues"
                onInput={(e) => setMatchEvent((e.target as HTMLInputElement).value)}
              />
            </label>
            {boardRun && (
              <label class="tf-field">
                <span class="tf-label">Item id from <span class="muted">(optional)</span></span>
                <Input
                  fullWidth
                  value={itemIdFrom}
                  placeholder="issue.id"
                  onInput={(e) => setItemIdFrom((e.target as HTMLInputElement).value)}
                />
              </label>
            )}
          </div>
          <label class="tf-field">
            <span class="tf-label">Only when the payload has <span class="muted">(optional)</span></span>
            <textarea
              class="tf-textarea mono"
              rows={3}
              placeholder={'action = labeled\nlabel.name = ai'}
              value={matchPayload}
              onInput={(e) => setMatchPayload((e.target as HTMLTextAreaElement).value)}
            />
            {match.error && <span class="tf-error">{match.error}</span>}
            <span class="tf-hint muted">
              One <span class="mono">dot.path = value</span> per line, exact match. Other deliveries,
              and GitHub's ping, start nothing.
            </span>
          </label>
          {boardRun && (
            <p class="tf-hint muted">
              GitHub does not redeliver a delivery refused because the workspace was busy. Pair
              this webhook with a nightly cron on the same board and tags: the cron skips anything
              already done.
            </p>
          )}
        </>
      )}

      {kind === 'page-watch' && (
        <>
          <label class="tf-field">
            <span class="tf-label">Page to watch</span>
            <Input
              fullWidth
              type="url"
              value={url}
              placeholder="https://github.com/you/repo/actions/workflows/ci.yml/badge.svg"
              onInput={(e) => setUrl((e.target as HTMLInputElement).value)}
            />
            {url && !urlOk && <span class="tf-error">Must start with http:// or https://</span>}
            <span class="tf-hint muted">
              Public pages only — internal and loopback addresses are refused. This
              version reads the page as sent, without running its JavaScript, so a
              status badge URL works where a JS dashboard may not.
            </span>
          </label>
          <label class="tf-field">
            <span class="tf-label">CSS selector <span class="muted">(optional)</span></span>
            <Input
              fullWidth
              value={selector}
              placeholder=".build-status"
              onInput={(e) => setSelector((e.target as HTMLInputElement).value)}
            />
            <span class="tf-hint muted">
              Narrows the watch to one part of the page, so clocks and ads elsewhere
              don't wake it. Supports tag, #id, .class, [attr="value"] and descendant
              chains.
            </span>
          </label>
          <label class="tf-check">
            <input
              type="checkbox"
              checked={includeContent}
              onChange={(e) => setIncludeContent((e.target as HTMLInputElement).checked)}
            />
            <span>
              Include page text in the prompt
              <span class="tf-hint muted">
                Off by default. The prompt gets the URL and what changed, but not the
                page's own words — a watched page is written by someone else, and its
                text reaches an agent that acts on it.
              </span>
            </span>
          </label>
        </>
      )}

      {(kind === 'cron' || kind === 'page-watch') && (
        <div class="tf-row">
          <label class="tf-field">
            <span class="tf-label">{kind === 'page-watch' ? 'Check every' : 'Cron schedule'}</span>
            <Input
              fullWidth
              value={schedule}
              onInput={(e) => setSchedule((e.target as HTMLInputElement).value)}
              placeholder="0 * * * *"
            />
            <span class="tf-hint muted">
              {kind === 'page-watch'
                ? 'Cron syntax. */5 * * * * checks every five minutes.'
                : 'Five fields: minute hour dom month dow.'}
            </span>
          </label>
          <label class="tf-field">
            <span class="tf-label">Timezone</span>
            <Input
              fullWidth
              value={timezone}
              onInput={(e) => setTimezone((e.target as HTMLInputElement).value)}
              placeholder="America/Los_Angeles"
            />
          </label>
        </div>
      )}

      {!boardRun && (
        <label class="tf-field">
          <span class="tf-label">Working directory</span>
          <Input
            fullWidth
            value={workdir}
            onInput={(e) => setWorkdir((e.target as HTMLInputElement).value)}
          />
        </label>
      )}

      <div class="tf-actions">
        <Button variant="ghost" type="button" onClick={onClose}>Cancel</Button>
        <Button variant="primary" type="submit" disabled={!valid || busy}>
          <Icon name="plus" size={14} /> Create {kind}
        </Button>
      </div>
    </form>
  );
}
