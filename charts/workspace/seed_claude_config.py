#!/usr/bin/env python3
"""Idempotent merger for the user's Claude Code config.

Maintains two files:

  ~/.claude.json
    - mcpServers.memory entry pointing at the stdio MCP server.

  ~/.claude/settings.json
    - Strips the legacy hooks.UserPromptSubmit memory-injection entry if
      present. That hook prepended a <workspace_memories> block to every
      prompt; it's now removed in favor of on-demand recall via the memory
      MCP tools (documented in CLAUDE.md). Removal is active so long-lived
      PVCs that still carry the old entry get cleaned up on boot.

Safe to run on every pod boot: leaves any other settings the user has
configured untouched, only repairs the kube-coder-managed keys.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

CONFIG_PATH = os.path.expanduser('~/.claude.json')
SETTINGS_PATH = os.path.expanduser('~/.claude/settings.json')

MCP_SCRIPT = '/home/dev/.claude-memory/mcp_memory.py'
INJECT_HOOK = '/home/dev/.claude-memory/memory_inject_hook.py'

# Default MCP servers seeded for every workspace. Keep this list small and
# generally useful — adding more bloats Claude's tool surface and slows
# startup. Users can add per-workspace entries by editing ~/.claude.json
# directly; the seeder only manages the keys it owns.
#
# All entries are stdio transports. Node-based servers run via `npx -y` so
# the actual package downloads lazily on first use, avoiding an image
# rebuild whenever the upstream version bumps.
DESIRED_MCPS = {
    # Our SQLite-backed persistent memory (provenance, history, MCP tools).
    'memory': {
        'type': 'stdio',
        'command': 'python3',
        'args': [MCP_SCRIPT],
    },
    # Agent orchestrator: spawn, monitor, and collect results from
    # sub-agent tasks (Ante, Claude, OpenCode). Runs alongside any agent.
    # See charts/workspace/mcp_agent_orchestrator.py for implementation.
    'agent-orchestrator': {
        'type': 'stdio',
        'command': 'python3',
        'args': ['/tmp/browser/mcp_agent_orchestrator.py'],
    },
    # Playwright: full browser automation. Useful for web-app testing /
    # scraping / e2e flows. Uses Firefox (already in the workspace image).
    # First-use cost is ~1 min while Playwright downloads its own browser
    # binaries into ~/.cache/ms-playwright (PVC-backed since ~ is on PVC).
    'playwright': {
        'type': 'stdio',
        'command': 'npx',
        'args': ['-y', '@playwright/mcp@latest', '--browser', 'firefox'],
    },
    # Sequential thinking: explicit chain-of-thought scratchpad tool.
    # Lightweight, Node-based, no external service.
    'sequential-thinking': {
        'type': 'stdio',
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-sequential-thinking'],
    },
}

def _atomic_write(path: str, data: str) -> None:
    parent = os.path.dirname(path) or '.'
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.claude-config-', dir=parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_json(path: str) -> dict:
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        print(f'[seed_claude_config] {path} is invalid JSON ({e}); refusing to overwrite',
              file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f'[seed_claude_config] {path} is not an object; refusing to overwrite',
              file=sys.stderr)
        sys.exit(1)
    return data


def _seed_mcp() -> None:
    cfg = _load_json(CONFIG_PATH)
    mcp = cfg.setdefault('mcpServers', {})
    if not isinstance(mcp, dict):
        print('[seed_claude_config] mcpServers is not an object; refusing to overwrite',
              file=sys.stderr)
        return

    changed = False
    for name, desired in DESIRED_MCPS.items():
        if mcp.get(name) == desired:
            continue
        mcp[name] = desired
        changed = True
        print(f'[seed_claude_config] updated mcpServers.{name}')

    if not changed:
        print(f'[seed_claude_config] all {len(DESIRED_MCPS)} default MCPs already correct')
        return
    _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2) + '\n')
    print(f'[seed_claude_config] wrote {CONFIG_PATH}')


def _entry_runs_inject_hook(entry: object) -> bool:
    """True if a UserPromptSubmit entry runs our memory-inject hook."""
    if not isinstance(entry, dict):
        return False
    for h in entry.get('hooks', []) or []:
        if INJECT_HOOK in (h or {}).get('command', ''):
            return True
    return False


def _remove_inject_hook() -> None:
    """Strip the legacy per-prompt memory-injection hook if present.

    The UserPromptSubmit hook fed a <workspace_memories> block into every
    Claude prompt. That's disabled now — memories are pulled on demand via
    the memory MCP tools. We actively remove any matching entry so a
    long-lived PVC's settings.json gets cleaned up on the next boot. Other
    UserPromptSubmit hooks the user configured are left intact.
    """
    if not os.path.exists(SETTINGS_PATH):
        return
    settings = _load_json(SETTINGS_PATH)
    hooks = settings.get('hooks')
    if not isinstance(hooks, dict):
        return
    submit_hooks = hooks.get('UserPromptSubmit')
    if not isinstance(submit_hooks, list):
        return

    kept = [e for e in submit_hooks if not _entry_runs_inject_hook(e)]
    if len(kept) == len(submit_hooks):
        return  # nothing of ours to remove

    if kept:
        hooks['UserPromptSubmit'] = kept
    else:
        hooks.pop('UserPromptSubmit', None)
        if not hooks:
            settings.pop('hooks', None)
    _atomic_write(SETTINGS_PATH, json.dumps(settings, indent=2) + '\n')
    print(f'[seed_claude_config] removed UserPromptSubmit memory hook from {SETTINGS_PATH}')


def main() -> int:
    _seed_mcp()
    _remove_inject_hook()
    return 0


if __name__ == '__main__':
    sys.exit(main())
