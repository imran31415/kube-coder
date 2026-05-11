#!/usr/bin/env python3
"""Idempotent merger for the user's Claude Code config.

Maintains two files:

  ~/.claude.json
    - mcpServers.memory entry pointing at the stdio MCP server.

  ~/.claude/settings.json
    - hooks.UserPromptSubmit entry pointing at the inject hook so every
      interactive `claude` prompt gets memory-aware context prefixed.

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

# UserPromptSubmit hooks fire on every user message. The hook script
# queries /api/memory and prints a <workspace_memories> block to stdout;
# Claude Code injects that into the prompt as additional context.
DESIRED_HOOK_ENTRY = {
    'matcher': '*',
    'hooks': [
        {'type': 'command', 'command': f'python3 {INJECT_HOOK}'},
    ],
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


def _hook_already_present(entries: list, target_cmd: str) -> bool:
    """Check whether any UserPromptSubmit entry already runs our hook."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for h in entry.get('hooks', []) or []:
            cmd = (h or {}).get('command', '')
            if target_cmd in cmd:
                return True
    return False


def _seed_hooks() -> None:
    settings = _load_json(SETTINGS_PATH)
    hooks = settings.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        print('[seed_claude_config] settings.hooks is not an object; refusing to overwrite',
              file=sys.stderr)
        return
    submit_hooks = hooks.setdefault('UserPromptSubmit', [])
    if not isinstance(submit_hooks, list):
        print('[seed_claude_config] settings.hooks.UserPromptSubmit not a list; refusing to overwrite',
              file=sys.stderr)
        return
    if _hook_already_present(submit_hooks, INJECT_HOOK):
        print('[seed_claude_config] UserPromptSubmit hook already present')
        return
    submit_hooks.append(DESIRED_HOOK_ENTRY)
    _atomic_write(SETTINGS_PATH, json.dumps(settings, indent=2) + '\n')
    print(f'[seed_claude_config] wrote UserPromptSubmit hook to {SETTINGS_PATH}')


def main() -> int:
    _seed_mcp()
    _seed_hooks()
    return 0


if __name__ == '__main__':
    sys.exit(main())
