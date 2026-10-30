import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { McpServerEntry, McpServerInput } from '../../api/mcpServers';

let servers: McpServerEntry[] = [];
const saved: McpServerInput[] = [];
const deleted: string[] = [];

vi.mock('../../api/mcpServers', async (orig) => ({
  ...(await orig<typeof import('../../api/mcpServers')>()),
  listMcpServers: () => Promise.resolve({ servers }),
  saveMcpServer: (entry: McpServerInput) => {
    saved.push(entry);
    return Promise.resolve({ ok: true as const, name: entry.name, sync: { claude: 'ok' } });
  },
  deleteMcpServer: (name: string) => {
    deleted.push(name);
    return Promise.resolve({ ok: true as const, sync: { claude: 'ok' } });
  },
}));

import { McpServersSection } from './McpServersSection';

function entry(over: Partial<McpServerEntry>): McpServerEntry {
  return {
    name: 'github',
    type: 'stdio',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-github'],
    env: { GITHUB_TOKEN: '…cd12' },
    url: '',
    headers: {},
    providers: ['ante', 'claude', 'codex', 'opencode'],
    enabled: true,
    ...over,
  };
}

function remoteEntry(over: Partial<McpServerEntry> = {}): McpServerEntry {
  return entry({
    name: 'canva',
    type: 'http',
    command: '',
    args: [],
    env: {},
    url: 'https://mcp.canva.com/mcp',
    headers: { Authorization: '…9876' },
    providers: ['claude'],
    ...over,
  });
}

describe('McpServersSection', () => {
  beforeEach(() => {
    servers = [];
    saved.length = 0;
    deleted.length = 0;
  });

  it('lists configured servers with command line and redacted env keys', async () => {
    servers = [entry({})];
    render(<McpServersSection />);
    expect(await screen.findByText('github')).toBeInTheDocument();
    expect(screen.getByText(/npx -y @modelcontextprotocol\/server-github/)).toBeInTheDocument();
    expect(screen.getByText(/env: GITHUB_TOKEN/)).toBeInTheDocument();
    expect(screen.getByText('enabled')).toBeInTheDocument();
    // Redacted hint values are never rendered as env values.
    expect(screen.queryByText('…cd12')).not.toBeInTheDocument();
  });

  it('adds a server from the form, splitting args and parsing env lines', async () => {
    render(<McpServersSection />);
    fireEvent.input(screen.getByPlaceholderText(/Name/), { target: { value: 'github' } });
    fireEvent.input(screen.getByPlaceholderText(/Command/), { target: { value: 'npx' } });
    fireEvent.input(screen.getByPlaceholderText(/Arguments/), { target: { value: '-y pkg' } });
    fireEvent.input(screen.getByPlaceholderText(/Env vars/), { target: { value: 'TOKEN=abc' } });
    fireEvent.click(screen.getByText('Add MCP server'));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0]).toEqual({
      name: 'github',
      type: 'stdio',
      command: 'npx',
      args: ['-y', 'pkg'],
      env: { TOKEN: 'abc' },
    });
  });

  it('rejects malformed env lines without calling the API', async () => {
    render(<McpServersSection />);
    fireEvent.input(screen.getByPlaceholderText(/Name/), { target: { value: 'x' } });
    fireEvent.input(screen.getByPlaceholderText(/Command/), { target: { value: 'run' } });
    fireEvent.input(screen.getByPlaceholderText(/Env vars/), { target: { value: 'notanenvline' } });
    fireEvent.click(screen.getByText('Add MCP server'));
    await waitFor(() => expect(saved).toHaveLength(0));
  });

  it('toggles enabled state, round-tripping blank env values', async () => {
    servers = [entry({})];
    render(<McpServersSection />);
    fireEvent.click(await screen.findByText('Disable'));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0].enabled).toBe(false);
    // Blank value = "keep the stored secret" on the server side.
    expect(saved[0].env).toEqual({ GITHUB_TOKEN: '' });
  });

  it('confirms before deleting a server', async () => {
    servers = [entry({})];
    render(<McpServersSection />);
    fireEvent.click(await screen.findByText('Remove'));
    // ConfirmDialog appears; delete only fires on confirm.
    expect(deleted).toHaveLength(0);
    fireEvent.click(screen.getByText('Remove server'));
    await waitFor(() => expect(deleted).toContain('github'));
  });

  it('adds a remote HTTP server by URL, parsing header lines (#815)', async () => {
    render(<McpServersSection />);
    fireEvent.click(screen.getByText('Remote HTTP'));
    expect(screen.queryByPlaceholderText(/Command/)).not.toBeInTheDocument();
    fireEvent.input(screen.getByPlaceholderText(/Name/), { target: { value: 'canva' } });
    const add = screen.getByText('Add MCP server').closest('button')!;
    expect(add).toBeDisabled(); // no URL yet
    fireEvent.input(screen.getByPlaceholderText(/URL/), {
      target: { value: 'https://mcp.canva.com/mcp' },
    });
    fireEvent.input(screen.getByPlaceholderText(/Headers/), {
      target: { value: 'Authorization: Bearer abc' },
    });
    fireEvent.click(add);
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0]).toEqual({
      name: 'canva',
      type: 'http',
      url: 'https://mcp.canva.com/mcp',
      headers: { Authorization: 'Bearer abc' },
    });
  });

  it('rejects malformed header lines without calling the API', async () => {
    render(<McpServersSection />);
    fireEvent.click(screen.getByText('Remote SSE'));
    fireEvent.input(screen.getByPlaceholderText(/Name/), { target: { value: 'x' } });
    fireEvent.input(screen.getByPlaceholderText(/URL/), { target: { value: 'https://x.example/sse' } });
    fireEvent.input(screen.getByPlaceholderText(/Headers/), { target: { value: 'noheader' } });
    fireEvent.click(screen.getByText('Add MCP server'));
    await waitFor(() => expect(saved).toHaveLength(0));
  });

  it('lists a remote server by URL and marks it Claude-only', async () => {
    servers = [remoteEntry()];
    render(<McpServersSection />);
    expect(await screen.findByText('canva')).toBeInTheDocument();
    expect(screen.getByText(/https:\/\/mcp\.canva\.com\/mcp/)).toBeInTheDocument();
    expect(screen.getByText(/headers: Authorization/)).toBeInTheDocument();
    expect(screen.getByText(/http · Claude only/)).toBeInTheDocument();
    expect(screen.queryByText('…9876')).not.toBeInTheDocument();
  });

  it('toggles a remote server, keeping its type and round-tripping blank headers', async () => {
    servers = [remoteEntry()];
    render(<McpServersSection />);
    fireEvent.click(await screen.findByText('Disable'));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0]).toEqual({
      name: 'canva',
      type: 'http',
      url: 'https://mcp.canva.com/mcp',
      headers: { Authorization: '' },
      enabled: false,
    });
  });
});
