import { apiGet, apiPost, apiDelete } from './client';

// User-defined MCP servers (Settings → MCP servers, issue #353). One canonical
// registry on the workspace disk, fanned out by the server to every
// MCP-capable assistant's native config (Claude, OpenCode, Ante, Codex).
// Shapes must match server.py's mcp-servers handlers / mcp_registry.py.

export interface McpServerEntry {
  name: string;
  command: string;
  args: string[];
  // Redacted: value is a last-4 hint like "…cc18" (or "•••"), never the real value.
  env: Record<string, string>;
  enabled: boolean;
}

// provider → 'ok' | 'skipped: …' | 'error: …'
export type McpSyncResults = Record<string, string>;

export interface McpServerInput {
  name: string;
  command: string;
  args: string[];
  // Real values on input. A blank value for an existing key keeps the stored
  // secret (the redacted list view round-trips without re-entering it).
  env: Record<string, string>;
  enabled?: boolean;
}

export const listMcpServers = () =>
  apiGet<{ servers: McpServerEntry[] }>('/api/mcp-servers');

export const saveMcpServer = (entry: McpServerInput) =>
  apiPost<{ ok: true; name: string; sync: McpSyncResults }>('/api/mcp-servers', entry);

export const deleteMcpServer = (name: string) =>
  apiDelete<{ ok: true; sync: McpSyncResults }>(`/api/mcp-servers/${encodeURIComponent(name)}`);
