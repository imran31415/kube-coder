import { apiGet, apiPost, apiDelete } from './client';

// User-defined MCP servers (Settings → MCP servers, issue #353). One canonical
// registry on the workspace disk, fanned out by the server to every
// MCP-capable assistant's native config (Claude, OpenCode, Ante, Codex).
// Shapes must match server.py's mcp-servers handlers / mcp_registry.py.

// stdio = a local command; http/sse = a hosted server by URL (#815), which
// reaches Claude only for now (see `providers`).
export type McpTransport = 'stdio' | 'http' | 'sse';

export interface McpServerEntry {
  name: string;
  type: McpTransport;
  // stdio fields — empty for a remote entry.
  command: string;
  args: string[];
  // Redacted: value is a last-4 hint like "…cc18" (or "•••"), never the real value.
  env: Record<string, string>;
  // Remote fields — empty for a stdio entry. Header values are redacted like env.
  url: string;
  headers: Record<string, string>;
  // Assistants this entry is applied to.
  providers: string[];
  enabled: boolean;
}

// provider → 'ok' | 'skipped: …' | 'error: …'
export type McpSyncResults = Record<string, string>;

export interface McpServerInput {
  name: string;
  // Omitted = keep the stored entry's transport (stdio for a new one).
  type?: McpTransport;
  command?: string;
  args?: string[];
  // Real values on input. A blank value for an existing key keeps the stored
  // secret (the redacted list view round-trips without re-entering it).
  // Same rule for `headers`.
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  enabled?: boolean;
}

export const listMcpServers = () =>
  apiGet<{ servers: McpServerEntry[] }>('/api/mcp-servers');

export const saveMcpServer = (entry: McpServerInput) =>
  apiPost<{ ok: true; name: string; sync: McpSyncResults }>('/api/mcp-servers', entry);

export const deleteMcpServer = (name: string) =>
  apiDelete<{ ok: true; sync: McpSyncResults }>(`/api/mcp-servers/${encodeURIComponent(name)}`);
