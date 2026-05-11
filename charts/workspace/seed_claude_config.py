#!/usr/bin/env python3
"""Idempotent merger for the user's Claude Code config.

Ensures ~/.claude.json contains an `mcpServers.memory` entry pointing at
/home/dev/.claude-memory/mcp_memory.py (stdio transport, plain python3).

Safe to run on every pod boot: leaves any other settings the user has
configured untouched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

CONFIG_PATH = os.path.expanduser('~/.claude.json')
MCP_SCRIPT = '/home/dev/.claude-memory/mcp_memory.py'

DESIRED = {
    'type': 'stdio',
    'command': 'python3',
    'args': [MCP_SCRIPT],
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


def main() -> int:
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    except json.JSONDecodeError as e:
        print(f'[seed_claude_config] {CONFIG_PATH} is invalid JSON ({e}); refusing to overwrite',
              file=sys.stderr)
        return 1

    if not isinstance(cfg, dict):
        print(f'[seed_claude_config] {CONFIG_PATH} is not an object; refusing to overwrite',
              file=sys.stderr)
        return 1

    mcp = cfg.setdefault('mcpServers', {})
    if not isinstance(mcp, dict):
        print('[seed_claude_config] mcpServers is not an object; refusing to overwrite',
              file=sys.stderr)
        return 1

    existing = mcp.get('memory')
    if existing == DESIRED:
        print('[seed_claude_config] mcpServers.memory already correct')
        return 0

    mcp['memory'] = DESIRED
    _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2) + '\n')
    print(f'[seed_claude_config] wrote mcpServers.memory to {CONFIG_PATH}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
