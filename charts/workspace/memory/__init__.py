"""Persistent memory subsystem for kube-coder workspaces.

Shared by:
  - server.py (HTTP /api/memory* surface for the dashboard)
  - mcp_memory.py (stdio MCP server invoked by Claude Code CLI)

Both processes operate on the same SQLite file at
/home/dev/.claude-memory/memory.db. WAL mode + BEGIN IMMEDIATE retries keep
the multi-writer story safe.
"""

from .store import MemoryStore, DB_PATH
from .manager import MemoryManager, MemoryError, NotFound, Conflict

__all__ = [
    'MemoryStore', 'DB_PATH',
    'MemoryManager', 'MemoryError', 'NotFound', 'Conflict',
]
