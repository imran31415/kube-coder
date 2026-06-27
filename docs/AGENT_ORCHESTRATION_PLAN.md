# Agent Orchestration System: Implementation Plan

## Overview

Build a real agent spawning system so running agents (Claude, Ante, OpenCode)
can spawn, monitor, and orchestrate sub-agent sessions via MCP tools.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Workspace Pod                              │
│                                                                   │
│  ┌──────────┐     ┌──────────────────┐                            │
│  │ Dashboard │────▶│ ClaudeTask        │── spawns tmux session ──▶│ Claude/Ante
│  │  (SPA)    │◀───│ Manager           │◀─ reads task.json ──────│ (spawning
│  └──────────┘     └──────────────────┘                            │  agent)
│                         │     ▲                                   │
│                         │     │ MCP stdio                          │
│                         ▼     │                                   │
│                  ┌──────────────────────┐                          │
│                  │ mcp_agent_            │                          │
│                  │ orchestrator.py      │── spawn_agent() ────────▶│ tmux session
│                  │ (MCP server)         │◀─ get_status/output ───│ (spawned Ante)
│                  │                      │                          │
│                  │ Tools:               │                          │
│                  │  • spawn_agent       │                          │
│                  │  • get_agent_status  │                          │
│                  │  • list_subagents    │                          │
│                  │  • get_agent_output  │                          │
│                  │  • wait_for_agent    │                          │
│                  │  • kill_agent        │                          │
│                  └──────────────────────┘                          │
└──────────────────────────────────────────────────────────────────┘
```

## Steps

### Step 1: Add `ante` as a spawnable assistant
**File:** `server.py`
**Change:** Add `ante` to `ASSISTANTS`, `assistant_command()`, `available_assistants()`
**Lines:** ~5

### Step 2: Add parent-child tracking
**File:** `server.py`
**Change:** Add `parent_task_id` param to `create_task()`, store in meta
**Lines:** ~5

### Step 3: Build MCP agent orchestrator
**File:** `mcp_agent_orchestrator.py` (NEW, ~200 lines)
**Change:** MCP server (stdio JSON-RPC 2.0) exposing 6 tools
**Tools:**
- `spawn_agent(prompt, assistant, workdir, parent_task_id)` → `{task_id, status}`
- `get_agent_status(task_id)` → `{task_id, status, ...}`
- `list_subagents(parent_task_id)` → `{sub_tasks: [...]}`
- `get_agent_output(task_id, tail)` → `{task_id, output, status}`
- `wait_for_agent(task_id, timeout, poll_interval)` → `{task_id, status, output_tail}`
- `kill_agent(task_id)` → `{task_id, status}`

### Step 4: Register MCP server
**File:** `seed_claude_config.py`
**Change:** Add `agent-orchestrator` entry to `DESIRED_MCPS`
**Lines:** ~5

### Step 5: Update CLAUDE.md
**File:** `claude-md.txt`
**Change:** Document agent orchestration MCP tools
**Lines:** ~20

### Step 6: Replace transcript scanner with real subagent API
**Files:**
- `transcript_scanner.py` — DELETE
- `server.py` — Remove import, remove old handler, add `?parent=` filter to `list_tasks`
- `api/tasks.ts` — Add `listSubAgents(parentId)` function
- `api/subagents.ts` — Rewrite to call new API

### Step 7: Rewrite SubagentsTab
**File:** `SubagentsTab.tsx`
**Change:** Query real child tasks instead of transcript scanner data

### Step 8: Ship new MCP file
**File:** `browser-configmap.yaml`
**Change:** Add `mcp_agent_orchestrator.py` to configmap keys
