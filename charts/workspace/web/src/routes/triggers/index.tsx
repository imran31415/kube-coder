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
import { createCron, createWebhook, createPageWatch, type Trigger, type TriggerKind } from '../../api/triggers';
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
              onClick={() => fire(t)}
              disabled={t.kind === 'page-watch' && t.suspended}
              title={
                t.kind === 'page-watch' && t.suspended
                  ? 'This watch is paused - resume it to check now'
                  : undefined
              }
            >
              <Icon name="play" size={12} /> {t.kind === 'page-watch' ? 'Check now' : 'Fire now'}
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
      <p class="trig-row-prompt muted">{t.prompt}</p>
      {(t.workdir || t.timezone) && (
        <div class="trig-row-meta muted mono">
          {t.workdir && <span>{t.workdir}</span>}
          {t.timezone && <span> · {t.timezone}</span>}
        </div>
      )}
      {showRuns && <TriggerRuns t={t} />}
    </article>
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
  const valid = idOk && prompt.trim().length > 0 && kindValid;

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    try {
      if (kind === 'cron') {
        await createCron({ id, schedule, prompt_template: prompt, workdir, timezone });
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
        const w = await createWebhook({ id, prompt_template: prompt, workdir });
        const secret = (w as { secret?: string }).secret;
        if (secret) {
          pushToast(`Webhook created. Secret: ${secret.slice(0, 12)}…`, { kind: 'success', ttl: 10000 });
        } else {
          pushToast('Webhook created', { kind: 'success' });
        }
      }
      await refreshTriggers();
      onClose();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Create failed', { kind: 'danger' });
    } finally {
      setBusy(false);
    }
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

      <label class="tf-field">
        <span class="tf-label">Working directory</span>
        <Input
          fullWidth
          value={workdir}
          onInput={(e) => setWorkdir((e.target as HTMLInputElement).value)}
        />
      </label>

      <div class="tf-actions">
        <Button variant="ghost" type="button" onClick={onClose}>Cancel</Button>
        <Button variant="primary" type="submit" disabled={!valid || busy}>
          <Icon name="plus" size={14} /> Create {kind}
        </Button>
      </div>
    </form>
  );
}
