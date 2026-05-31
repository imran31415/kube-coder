#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: prepend relevant memories to every prompt.

How it fits in:
  ~/.claude/settings.json registers this script under the
  `UserPromptSubmit` hook. On every prompt the user types into an
  interactive `claude` session, Claude Code:
    1. Pipes a JSON payload to this script's stdin.
    2. Captures this script's stdout and inserts it as `additionalContext`
       in the model's view of the prompt.

  This complements the dashboard-side auto-injection (in
  ClaudeTaskManager.create_task) which only fires for tasks spawned via
  the dashboard API. Together they cover every Claude invocation in the
  workspace: terminal-spawned interactive sessions AND dashboard tasks.

Design constraints:
  - Never block the user's prompt. Any error → exit 0 with empty stdout.
  - Bounded latency (< 3 s timeout on the HTTP call).
  - Bounded output (< 4 KiB) so we don't bloat context.
  - Honor `secret`-tagged entries (excluded from auto-inject).
  - Be silent if memory is empty (no "(no memories found)" noise).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

API_BASE = 'http://localhost:6080/api/memory'
TOKEN_FILE = '/home/dev/.claude-tasks/.api-token'
TIMEOUT = 3.0
MAX_ENTRIES = 8
MAX_CHARS = 4000
# Match the documented relevance floor (see values.yaml memory.inject.minScore
# and MemoryManager.top_for_prompt). Below this the auto-inject prepends
# noise; the dashboard's create_task path enforces the same floor.
MIN_SCORE = 0.30


def _read_token() -> str:
    try:
        with open(TOKEN_FILE, 'r') as f:
            return f.read().strip()
    except OSError:
        return ''


def _read_prompt() -> str:
    """Claude Code passes a JSON payload via stdin: {prompt, session_id, ...}.

    Older versions may pass the raw prompt as plain text. Handle both.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return ''
    if not raw:
        return os.environ.get('CLAUDE_USER_PROMPT', '')
    raw = raw.strip()
    # Try JSON first.
    if raw.startswith('{'):
        try:
            data = json.loads(raw)
            return str(data.get('prompt') or data.get('user_prompt') or '').strip()
        except json.JSONDecodeError:
            pass
    return raw


def _query_memories(prompt: str, token: str) -> list:
    if not prompt or not token:
        return []
    q = urllib.parse.quote(prompt[:512])
    url = f'{API_BASE}?q={q}&limit={MAX_ENTRIES}'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
    except Exception:
        return []
    memories = data.get('memories', []) or []
    # Enforce the documented relevance floor. We keep entries whose score is
    # absent (the search degraded to LIKE fallback, or the endpoint returned
    # an unscored list) so we never silently drop everything when scoring is
    # unavailable; only entries scored below MIN_SCORE are filtered.
    def _keep(m):
        score = m.get('_score')
        return score is None or score >= MIN_SCORE
    return [m for m in memories if _keep(m)]


def _format_block(memories: list) -> str:
    if not memories:
        return ''
    lines = ['<workspace_memories>',
             'You previously remembered these. Treat as authoritative '
             'prior context; consult before saying you do not know. '
             'When in doubt about facts the user may have told you, '
             'call the `memory_search` tool instead of guessing.']
    budget = MAX_CHARS
    used = 0
    for m in memories:
        tags = (m.get('tags') or '').split(',')
        if 'secret' in (t.strip() for t in tags):
            continue
        ns = m.get('namespace') or ''
        key = m.get('key') or ''
        value = (m.get('value') or '').replace('\n', ' ').strip()
        # Trim individual values so one long entry can't dominate.
        if len(value) > 280:
            value = value[:280].rstrip() + '…'
        line = f'- [{ns}.{key}] {value}'
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line) + 1
    lines.append('</workspace_memories>')
    return '\n'.join(lines)


def main() -> int:
    try:
        prompt = _read_prompt()
        token = _read_token()
        memories = _query_memories(prompt, token)
        block = _format_block(memories)
        if block:
            sys.stdout.write(block)
            sys.stdout.write('\n')
    except Exception:
        # Failsafe: never break the user's prompt flow.
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
