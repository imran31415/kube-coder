import { useEffect, useState } from 'preact/hooks';
import {
  listMcpServers,
  saveMcpServer,
  deleteMcpServer,
  type McpServerEntry,
  type McpServerInput,
  type McpSyncResults,
  type McpTransport,
} from '../../api/mcpServers';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { Pill } from '../../components/primitives/Pill';
import { Icon } from '../../components/Icon';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { pushToast } from '../../store/ui';

// Parse "KEY=VALUE" lines into an env object; invalid lines are reported.
function parseEnvLines(text: string): { env: Record<string, string>; error: string | null } {
  const env: Record<string, string> = {};
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) return { env, error: `Env lines must be KEY=VALUE (got "${line}")` };
    env[line.slice(0, eq).trim()] = line.slice(eq + 1);
  }
  return { env, error: null };
}

// Parse "Name: value" header lines (#815); invalid lines are reported.
function parseHeaderLines(text: string): { headers: Record<string, string>; error: string | null } {
  const headers: Record<string, string> = {};
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const colon = line.indexOf(':');
    if (colon <= 0) return { headers, error: `Header lines must be Name: value (got "${line}")` };
    headers[line.slice(0, colon).trim()] = line.slice(colon + 1).trim();
  }
  return { headers, error: null };
}

const TRANSPORTS: { id: McpTransport; label: string }[] = [
  { id: 'stdio', label: 'Local command' },
  { id: 'http', label: 'Remote HTTP' },
  { id: 'sse', label: 'Remote SSE' },
];

function isRemote(t: McpTransport | undefined): boolean {
  return t === 'http' || t === 'sse';
}

function syncWarning(sync: McpSyncResults | undefined): string | null {
  if (!sync) return null;
  const bad = Object.entries(sync).filter(([, r]) => r.startsWith('error'));
  if (!bad.length) return null;
  return `Applied, but some assistants failed to update: ${bad.map(([p]) => p).join(', ')}`;
}

export function McpServersSection() {
  const [servers, setServers] = useState<McpServerEntry[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [transport, setTransport] = useState<McpTransport>('stdio');
  const [name, setName] = useState('');
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [envText, setEnvText] = useState('');
  const [url, setUrl] = useState('');
  const [headerText, setHeaderText] = useState('');
  const remote = isRemote(transport);

  async function refresh() {
    try {
      const r = await listMcpServers();
      setServers(r.servers);
    } catch {
      // server unavailable (or older image) — leave list null, section still renders
    }
  }
  useEffect(() => { void refresh(); }, []);

  function toastResult(msg: string, sync: McpSyncResults | undefined) {
    const warn = syncWarning(sync);
    if (warn) pushToast(warn, { kind: 'warn' });
    else pushToast(msg, { kind: 'success' });
  }

  async function onAdd() {
    let entry: McpServerInput;
    if (remote) {
      const { headers, error } = parseHeaderLines(headerText);
      if (error) {
        pushToast(error, { kind: 'danger' });
        return;
      }
      entry = { name: name.trim(), type: transport, url: url.trim(), headers };
    } else {
      const { env, error } = parseEnvLines(envText);
      if (error) {
        pushToast(error, { kind: 'danger' });
        return;
      }
      entry = {
        name: name.trim(),
        type: 'stdio',
        command: command.trim(),
        args: args.trim() ? args.trim().split(/\s+/) : [],
        env,
      };
    }
    setBusy('add');
    try {
      const r = await saveMcpServer(entry);
      toastResult('MCP server saved', r.sync);
      setName(''); setCommand(''); setArgs(''); setEnvText('');
      setUrl(''); setHeaderText('');
      await refresh();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Save failed', { kind: 'danger' });
    } finally {
      setBusy(null);
    }
  }

  async function onToggle(s: McpServerEntry) {
    setBusy(s.name);
    try {
      // Blank env/header values round-trip the stored secrets unchanged.
      const blank = (o: Record<string, string>) =>
        Object.fromEntries(Object.keys(o).map((k) => [k, '']));
      const r = await saveMcpServer(
        isRemote(s.type)
          ? { name: s.name, type: s.type, url: s.url, headers: blank(s.headers), enabled: !s.enabled }
          : { name: s.name, type: 'stdio', command: s.command, args: s.args, env: blank(s.env), enabled: !s.enabled },
      );
      toastResult(s.enabled ? 'MCP server disabled' : 'MCP server enabled', r.sync);
      await refresh();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Update failed', { kind: 'danger' });
    } finally {
      setBusy(null);
    }
  }

  async function onDelete(serverName: string) {
    setBusy(serverName);
    setDeleteTarget(null);
    try {
      const r = await deleteMcpServer(serverName);
      toastResult('MCP server removed', r.sync);
      await refresh();
    } catch (err) {
      pushToast(err instanceof Error ? err.message : 'Delete failed', { kind: 'danger' });
    } finally {
      setBusy(null);
    }
  }

  return (
    <section class="settings-section">
      <h2 class="settings-section-title">MCP servers</h2>
      <p class="settings-row-hint muted">
        Add Model Context Protocol connectors for every assistant in this workspace
        (Claude, OpenCode, Ante, Codex). Stored on your workspace disk and applied to
        each assistant's own config — new sessions pick them up. Remote (HTTP/SSE)
        servers reach Claude only for now. Built-in workspace servers (memory,
        dashboard, …) and the Hypervisor's fixed set are managed for you and can't
        be changed here.
      </p>

      {servers && servers.length > 0 && (
        <div class="settings-subs">
          {servers.map((s) => (
            <div class="settings-sub-row" key={s.name}>
              <div class="settings-sub-label">
                <span class="settings-sub-name">{s.name}</span>
                <Pill tone={s.enabled ? 'success' : 'neutral'} mono>
                  {s.enabled ? 'enabled' : 'disabled'}
                </Pill>
                {isRemote(s.type) && (
                  <Pill tone="neutral" mono>
                    {s.type} · Claude only
                  </Pill>
                )}
              </div>
              <div class="settings-sub-control" style={{ gap: 'var(--size-2)' }}>
                <Button
                  variant="secondary"
                  type="button"
                  disabled={busy === s.name}
                  onClick={() => onToggle(s)}
                >
                  {s.enabled ? 'Disable' : 'Enable'}
                </Button>
                <Button
                  variant="secondary"
                  type="button"
                  disabled={busy === s.name}
                  onClick={() => setDeleteTarget(s.name)}
                >
                  <Icon name="trash" size={14} /> Remove
                </Button>
              </div>
              <div class="settings-sub-note settings-radio-hint muted" style={{ fontFamily: 'var(--font-mono)' }}>
                {isRemote(s.type) ? (
                  <>
                    {s.url}
                    {Object.keys(s.headers).length > 0 && ` · headers: ${Object.keys(s.headers).join(', ')}`}
                  </>
                ) : (
                  <>
                    {s.command} {s.args.join(' ')}
                    {Object.keys(s.env).length > 0 && ` · env: ${Object.keys(s.env).join(', ')}`}
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      <div class="settings-row">
        <div class="settings-row-label">
          Add server
          <div class="settings-radio-hint muted">
            {remote ? (
              <>
                Name, the server's https URL, and optional "Name: value" header
                lines (values are stored securely and never shown back). For an
                OAuth server (e.g. Canva), sign in once from a Claude session:
                run <code>/mcp</code>, pick the server, Authenticate — if the
                redirect page fails to load, paste its URL back into Claude.
              </>
            ) : (
              <>
                Name, the command to launch it, optional arguments, and optional
                KEY=VALUE env lines (values are stored securely and never shown back).
              </>
            )}
          </div>
        </div>
        <div class="settings-row-control settings-row-control-stack" style={{ gap: 'var(--size-2)' }}>
          <div class="seg" role="group" aria-label="Server type">
            {TRANSPORTS.map((t) => (
              <button
                key={t.id}
                type="button"
                class={`seg-item ${transport === t.id ? 'seg-item-active' : ''}`}
                aria-pressed={transport === t.id}
                onClick={() => setTransport(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>
          <Input
            fullWidth
            value={name}
            placeholder={remote ? 'Name (e.g. canva)' : 'Name (e.g. github)'}
            onInput={(e) => setName((e.target as HTMLInputElement).value)}
          />
          {remote ? (
            <>
              <Input
                fullWidth
                value={url}
                placeholder="URL (e.g. https://mcp.canva.com/mcp)"
                onInput={(e) => setUrl((e.target as HTMLInputElement).value)}
              />
              <textarea
                class="input input-full"
                rows={2}
                value={headerText}
                placeholder={'Headers, one per line (optional, e.g. Authorization: Bearer …)'}
                onInput={(e) => setHeaderText((e.target as HTMLTextAreaElement).value)}
              />
            </>
          ) : (
            <>
              <Input
                fullWidth
                value={command}
                placeholder="Command (e.g. npx)"
                onInput={(e) => setCommand((e.target as HTMLInputElement).value)}
              />
              <Input
                fullWidth
                value={args}
                placeholder="Arguments (e.g. -y @modelcontextprotocol/server-github)"
                onInput={(e) => setArgs((e.target as HTMLInputElement).value)}
              />
              <textarea
                class="input input-full"
                rows={2}
                value={envText}
                placeholder={'Env vars, one per line (e.g. GITHUB_TOKEN=ghp_…)'}
                onInput={(e) => setEnvText((e.target as HTMLTextAreaElement).value)}
              />
            </>
          )}
          <div>
            <Button
              variant="primary"
              type="button"
              disabled={busy === 'add' || !name.trim() || !(remote ? url.trim() : command.trim())}
              onClick={onAdd}
            >
              <Icon name="plus" size={14} /> Add MCP server
            </Button>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Remove MCP server?"
        body={
          deleteTarget
            ? `This removes "${deleteTarget}" from every assistant's config. Built-in workspace servers are unaffected.`
            : ''
        }
        confirmLabel="Remove server"
        destructive
        onConfirm={() => deleteTarget && onDelete(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
      />
    </section>
  );
}
