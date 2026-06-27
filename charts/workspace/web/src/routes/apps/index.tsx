import { useEffect, useState } from 'preact/hooks';
import { signal } from '@preact/signals';
import { currentPath, navigate, pathSuffix } from '../../store/router';
import { pushToast } from '../../store/ui';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { EmptyState } from '../../components/primitives/EmptyState';
import { MutatorOnly } from '../../components/MutatorOnly';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { Modal } from '../../components/Modal';
import {
  type AppEntry,
  listApps,
  pinApp,
  unpinApp,
  proxyUrl,
} from '../../api/apps';
import { ApiError } from '../../api/client';
import { AppEmbed } from './AppEmbed';
import './apps.css';

// Top-level signals — survive across mounts of the route component so
// the list doesn't blank-flash every time the user navigates between
// /apps and /apps/<port>.
const apps = signal<AppEntry[]>([]);
const loaded = signal<boolean>(false);
const error = signal<string | null>(null);
const unavailable = signal<string | null>(null);

async function refresh() {
  try {
    const res = await listApps();
    apps.value = res.apps;
    unavailable.value = res.unavailable_reason;
    error.value = null;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loaded.value = true;
  }
}

export function AppsRoute() {
  // Sub-path support: `/apps`         → list view
  //                  `/apps/<port>`   → embedded iframe for that app
  const suffix = pathSuffix(currentPath.value).split('/')[0];
  const embedPort = /^\d+$/.test(suffix) ? Number(suffix) : null;

  useEffect(() => {
    void refresh();
    // The /proc/net/tcp[6] snapshot can change every few seconds when
    // the user is starting/stopping dev servers. A quiet 5s refresh
    // surfaces "stopped" → "running" transitions without being so
    // chatty it hammers the loopback parse on every tick.
    const id = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void refresh();
    }, 5000);
    return () => window.clearInterval(id);
  }, []);

  if (embedPort !== null) {
    return <AppEmbed port={embedPort} appsList={apps.value} />;
  }
  return <AppsList />;
}

function AppsList() {
  const [pinDraft, setPinDraft] = useState<{ port: string; name: string; strip_prefix: boolean } | null>(null);
  const [confirmUnpin, setConfirmUnpin] = useState<AppEntry | null>(null);

  const rows = apps.value;
  return (
    <div class="route route-apps">
      <header class="route-header route-header-with-action">
        <div>
          <h1 class="route-title">Applications</h1>
          <p class="route-subtitle muted">
            Locally-listening services discovered on this workspace. Pin a
            port to give it a friendly name and access it from the dashboard.
          </p>
        </div>
        <MutatorOnly>
          <Button variant="primary" size="md" onClick={() =>
            setPinDraft({ port: '', name: '', strip_prefix: false })
          }>
            <Icon name="plus" size={14} /> Pin port
          </Button>
        </MutatorOnly>
      </header>

      {unavailable.value && (
        <div class="apps-banner" role="status">
          <strong>Embedded apps disabled.</strong> {unavailable.value}{' '}
          You can still open apps in a new tab.
        </div>
      )}
      {error.value && <div class="apps-error" role="alert">{error.value}</div>}

      {loaded.value && rows.length === 0 ? (
        <EmptyState
          icon={<Icon name="link" size={24} />}
          title="No apps detected"
          description="Start a local server on this workspace (e.g. python3 -m http.server 8000, npm run dev) and it'll appear here. Pin a port to keep it listed even when stopped."
        />
      ) : (
        <ul class="apps-list" aria-label="Applications">
          {rows.map((a) => (
            <AppRow
              key={a.port}
              app={a}
              canEmbed={!unavailable.value}
              onUnpin={() => setConfirmUnpin(a)}
            />
          ))}
        </ul>
      )}

      {pinDraft && (
        <PinDialog
          draft={pinDraft}
          onChange={setPinDraft}
          onClose={() => setPinDraft(null)}
          onSaved={() => { setPinDraft(null); void refresh(); }}
        />
      )}

      {confirmUnpin && (
        <ConfirmDialog
          open
          title={`Unpin port ${confirmUnpin.port}?`}
          body={`"${confirmUnpin.name}" will be removed from the list. The app keeps running; only the pin record is deleted.`}
          confirmLabel="Unpin"
          destructive
          onCancel={() => setConfirmUnpin(null)}
          onConfirm={async () => {
            const port = confirmUnpin.port;
            setConfirmUnpin(null);
            try {
              await unpinApp(port);
              await refresh();
              pushToast(`Unpinned port ${port}`, { kind: 'success' });
            } catch (e) {
              const msg = e instanceof Error ? e.message : String(e);
              pushToast(`Failed to unpin: ${msg}`, { kind: 'danger', ttl: 6000 });
            }
          }}
        />
      )}
    </div>
  );
}

function AppRow({
  app,
  canEmbed,
  onUnpin,
}: {
  app: AppEntry;
  canEmbed: boolean;
  onUnpin: () => void;
}) {
  const blocked = app.status === 'blocked';
  const stopped = app.status === 'stopped';
  return (
    <li class={`apps-row apps-row-${app.status}`}>
      <div class="apps-row-main">
        <div class="apps-row-name">
          {app.name || <span class="muted">port {app.port}</span>}
          {app.pinned && <Pill tone="info">pinned</Pill>}
          <StatusPill status={app.status} />
        </div>
        <div class="apps-row-meta muted">
          :{app.port}
          {app.addr && app.addr !== '127.0.0.1' && <> · {app.addr}</>}
          {app.strip_prefix && <> · keeps proxy prefix</>}
        </div>
      </div>
      <div class="apps-row-actions">
        {!blocked && (
          <>
            <Button
              variant="secondary"
              size="sm"
              disabled={stopped || !canEmbed}
              onClick={() => navigate(`/apps/${app.port}`)}
              title={
                !canEmbed
                  ? 'Embedding requires oauth2 auth mode'
                  : stopped
                    ? 'App is not currently listening'
                    : 'Open in dashboard'
              }
            >
              Open here
            </Button>
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              disabled={stopped}
              onClick={() => window.open(proxyUrl(app.port), '_blank', 'noopener')}
              aria-label="Open in new tab"
              title="Open in new tab"
            >
              <Icon name="link" size={14} />
            </Button>
          </>
        )}
        {app.pinned && (
          <MutatorOnly>
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              onClick={onUnpin}
              aria-label="Unpin"
              title="Unpin"
            >
              <Icon name="close" size={14} />
            </Button>
          </MutatorOnly>
        )}
      </div>
    </li>
  );
}

function StatusPill({ status }: { status: AppEntry['status'] }) {
  if (status === 'running') return <Pill tone="success">running</Pill>;
  if (status === 'stopped') return <Pill tone="warn">stopped</Pill>;
  return <Pill tone="danger">reserved</Pill>;
}

function PinDialog({
  draft,
  onChange,
  onClose,
  onSaved,
}: {
  draft: { port: string; name: string; strip_prefix: boolean };
  onChange: (next: { port: string; name: string; strip_prefix: boolean }) => void;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    const port = Number(draft.port);
    if (!Number.isFinite(port) || port < 1 || port > 65535) {
      setErr('Port must be between 1 and 65535');
      return;
    }
    if (!draft.name.trim()) {
      setErr('Name is required');
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      await pinApp({ port, name: draft.name.trim(), strip_prefix: draft.strip_prefix });
      pushToast(`Pinned port ${port}`, { kind: 'success' });
      onSaved();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open onClose={onClose} label="Pin a port" width={440}>
      <h2 class="apps-modal-title">Pin a port</h2>
      <p class="muted apps-modal-hint">
        Pinned ports stay listed even when the app isn't running, and you
        can give them a friendly name.
      </p>
      <div class="apps-modal-row">
        <label class="apps-modal-label">
          Port
          <Input
            type="number"
            min={1}
            max={65535}
            value={draft.port}
            onInput={(e) =>
              onChange({ ...draft, port: (e.target as HTMLInputElement).value })
            }
            placeholder="e.g. 3000"
          />
        </label>
        <label class="apps-modal-label">
          Name
          <Input
            value={draft.name}
            onInput={(e) =>
              onChange({ ...draft, name: (e.target as HTMLInputElement).value })
            }
            placeholder="e.g. Django app"
            maxLength={80}
          />
        </label>
      </div>
      <label class="apps-modal-check">
        <input
          type="checkbox"
          checked={draft.strip_prefix}
          onChange={(e) =>
            onChange({ ...draft, strip_prefix: (e.target as HTMLInputElement).checked })
          }
        />
        <span>
          Keep proxy prefix (for Vite-style dev servers configured with{' '}
          <code>--base /api/app-proxy/&lt;port&gt;/</code>)
        </span>
      </label>
      {err && <div class="apps-modal-err" role="alert">{err}</div>}
      <div class="apps-modal-actions">
        <Button variant="ghost" onClick={onClose} disabled={saving}>
          Cancel
        </Button>
        <Button variant="primary" onClick={submit} disabled={saving}>
          {saving ? 'Pinning…' : 'Pin port'}
        </Button>
      </div>
    </Modal>
  );
}
