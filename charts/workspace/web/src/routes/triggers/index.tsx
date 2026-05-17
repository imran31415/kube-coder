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
import { createCron, createWebhook, type Trigger, type TriggerKind } from '../../api/triggers';
import { Button } from '../../components/primitives/Button';
import { MutatorOnly } from '../../components/MutatorOnly';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { EmptyState } from '../../components/primitives/EmptyState';
import { Drawer } from '../../components/Drawer';
import { BottomSheet } from '../../components/BottomSheet';
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
            Webhooks and crons in one list. {triggers.value.length} trigger{triggers.value.length === 1 ? '' : 's'}.
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
          placeholder="Filter by id, prompt, or schedule…"
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
              : 'Triggers fire Claude tasks automatically — on a schedule, via webhook, or manually.'
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

function TriggerRow({ t }: { t: Trigger }) {
  const tone = t.kind === 'cron' ? (t.suspended ? 'warn' : 'accent') : 'info';
  const label = t.kind === 'cron' ? (t.suspended ? 'cron · paused' : 'cron') : 'webhook';
  return (
    <article class="trig-row">
      <div class="trig-row-head">
        <Pill tone={tone} mono>{label}</Pill>
        <span class="trig-row-id mono">{t.id}</span>
        {t.schedule && <span class="trig-row-sched mono">{t.schedule}</span>}
        <div class="trig-row-actions">
          <MutatorOnly>
            <Button size="sm" variant="ghost" onClick={() => fire(t)}>
              <Icon name="play" size={12} /> Fire now
            </Button>
            {t.kind === 'cron' && (
              <Button size="sm" variant="ghost" onClick={() => toggleSuspend(t)}>
                {t.suspended ? 'Resume' : 'Pause'}
              </Button>
            )}
            <Button
              size="sm"
              variant="danger"
              onClick={() => {
                if (confirm(`Delete ${t.id}?`)) void removeTrigger(t);
              }}
            >
              Delete
            </Button>
          </MutatorOnly>
        </div>
      </div>
      <p class="trig-row-prompt muted">{t.prompt}</p>
      {(t.workdir || t.timezone) && (
        <div class="trig-row-meta muted mono">
          {t.workdir && <span>{t.workdir}</span>}
          {t.timezone && <span> · {t.timezone}</span>}
        </div>
      )}
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
  const [busy, setBusy] = useState(false);

  const idOk = /^[a-z0-9-]+$/.test(id);
  const valid = idOk && prompt.trim().length > 0 && (kind === 'webhook' || schedule.trim().split(/\s+/).length === 5);

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    try {
      if (kind === 'cron') {
        await createCron({ id, schedule, prompt_template: prompt, workdir, timezone });
        pushToast('Cron created', { kind: 'success' });
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
        <div class="seg">
          {(['cron', 'webhook'] as TriggerKind[]).map((k) => (
            <button
              key={k}
              type="button"
              class={`seg-item ${kind === k ? 'seg-item-active' : ''}`}
              onClick={() => setKind(k)}
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

      {kind === 'cron' && (
        <div class="tf-row">
          <label class="tf-field">
            <span class="tf-label">Cron schedule</span>
            <Input
              fullWidth
              value={schedule}
              onInput={(e) => setSchedule((e.target as HTMLInputElement).value)}
              placeholder="0 * * * *"
            />
            <span class="tf-hint muted">Five fields: minute hour dom month dow.</span>
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
