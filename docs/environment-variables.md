# Environment Variables

This document describes all environment variables used by kube-coder components. Environment variables can be configured in Helm values files, Kubernetes ConfigMaps, or directly in container specifications.

## Workspace Server (`server.py`)

### Core Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_MODE` | `basic` | Authentication mode. Options: `basic` (username/password), `oauth` (GitHub OAuth), `none` (disabled for local development). |
| `READONLY_MODE` | `false` | When `true`, all write operations (task creation, memory updates, file uploads) are disabled. |
| `TRUSTED_PROXY` | `true` | When `true`, trust `X-Forwarded-*` and `Remote-User` headers from the proxy. **Security:** Set to `false` if your ingress doesn't strip these headers. |
| `ALLOW_INTERNAL_HOOKS` | `false` | When `true`, allow webhooks and cron jobs to fire tasks on the same workspace (single-user/trusted deployments only). |
| `DEMO_SHOW_ALL` | `false` | When `true`, show all UI elements in demo mode regardless of actual configuration. |
| `MAX_REQUEST_BODY_BYTES` | `1048576` (1MB) | Maximum allowed request body size in bytes. |
| `STREAM_MAX_SECONDS` | `1800` (30 min) | Maximum duration for SSE stream connections. |
| `WORKSPACE_USER` | `unknown` | Username of the workspace owner (used for display purposes). |
| `POD_NAMESPACE` | `coder` | Kubernetes namespace where the workspace pod runs. |
| `DOCS_DIR` | `/home/dev/.kube-coder/docs` | Directory containing documentation files. |

### AI Assistant Configuration

#### Claude Code (Default)
| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | - | **Required for Claude.** Anthropic API key for Claude Code access. |

#### OpenRouter Integration
| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | - | OpenRouter API key for alternative AI providers. |
| `KC_OPENROUTER_MODEL` | `anthropic/claude-sonnet-4` | Model identifier for OpenRouter (e.g., `anthropic/claude-3-5-sonnet`). |

#### DeepSeek Integration  
| Variable | Default | Description |
|----------|---------|-------------|
| `DEEPSEEK_API_KEY` | - | DeepSeek API key. |
| `KC_DEEPSEEK_MODEL` | `deepseek-chat` | DeepSeek model identifier. |

#### Fallback/Generic AI Backend
| Variable | Default | Description |
|----------|---------|-------------|
| `KC_FALLBACK_BASE_URL` | - | Base URL for generic OpenAI-compatible API (e.g., `http://localhost:11434/v1` for Ollama). |
| `KC_FALLBACK_API_KEY` | - | API key for fallback backend. |
| `KC_FALLBACK_MODEL` | `qwen3:32b-q4_K_M` | Model identifier for fallback backend. |
| `KC_FALLBACK_PROVIDER_ID` | - | Provider identifier for fallback backend (display only). |
| `KC_FALLBACK_PROVIDER_NAME` | - | Provider name for fallback backend (display only). |

#### Harness-Specific Overrides
| Variable | Default | Description |
|----------|---------|-------------|
| `KC_HARNESS_MODEL` | - | Model override for harness tasks (takes precedence over `KC_FALLBACK_MODEL`). |
| `KC_HARNESS_BASE_URL` | - | Base URL override for harness tasks. |
| `KC_HARNESS_API_KEY` | - | API key override for harness tasks. |

### Memory System
| Variable | Default | Description |
|----------|---------|-------------|
| `KC_MEMORY_PREINJECT` | `false` | When `true` or `1`, pre-inject relevant memories into new task prompts (legacy behavior). |
| `KC_TASK_ID` | - | **Automatically set.** Task identifier passed to spawned tmux sessions for memory provenance. |

### Task & Agent Orchestration
| Variable | Default | Description |
|----------|---------|-------------|
| `KC_MAX_SUBAGENTS` | `8` | Maximum number of concurrent subagents. |
| `KC_MAX_SPAWN_DEPTH` | `3` | Maximum agent spawning depth (to prevent infinite recursion). |
| `KC_AGENT_DEPTH` | `0` | **Automatically set.** Current agent depth (incremented for subagents). |

### Dashboard & UI
| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_DIST_DIR` | `/opt/dashboard-dist` | Directory containing compiled dashboard assets. |
| `DISPLAY` | `:99` | X11 display for browser/VNC sessions. |

## Workspace Controller (`controller.py`)

### Core Configuration
| Variable | Default | Description |
|----------|---------|-------------|
| `CONTROLLER_PORT` | `8080` | HTTP port for the controller API. |
| `WORKSPACE_PREFIX` | `ws-` | Prefix for workspace resource names. |
| `NAMESPACE` | `coder` | Kubernetes namespace where workspaces are deployed. |
| `ADMIN_USERS` | - | Comma-separated list of GitHub usernames with admin access. |
| `CONTROLLER_DEV_TOKEN` | - | Development token for bypassing auth in local dev. |
| `CONTROLLER_DIST_DIR` | `/controller-web` | Directory containing controller web UI assets. |

### Request Limits
| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_REQUEST_BODY_BYTES` | `65536` (64KB) | Maximum request body size for controller API. |
| `KUBECTL_TIMEOUT` | `15` | Timeout in seconds for `kubectl` commands. |

### Monitoring & Metrics
| Variable | Default | Description |
|----------|---------|-------------|
| `PROMETHEUS_URL` | - | Prometheus server URL for metrics collection. |
| `PROM_TIMEOUT` | `8` | Timeout in seconds for Prometheus queries. |

### Cost Estimation
| Variable | Default | Description |
|----------|---------|-------------|
| `COST_CPU_CORE_HOUR` | `0.0082` | Cost per CPU core hour (USD). |
| `COST_MEM_GB_HOUR` | `0.0041` | Cost per GB memory hour (USD). |
| `COST_STORAGE_GB_MONTH` | `0.10` | Cost per GB storage month (USD). |

### Resource Insights
| Variable | Default | Description |
|----------|---------|-------------|
| `INSIGHTS_WINDOW_SECONDS` | `21600` (6h) | Time window for resource utilization insights. |
| `INSIGHTS_IDLE_CPU_CORES` | `0.05` | CPU threshold (cores) below which a pod is considered idle. |

## Harness (`harness.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `PWD` | - | Current working directory (falls back to `os.getcwd()`). |
| `KC_HARNESS_MODEL` | - | Model override for harness execution. |
| `KC_FALLBACK_MODEL` | - | Fallback model if `KC_HARNESS_MODEL` not set. |
| `KC_HARNESS_BASE_URL` | - | Base URL override for harness. |
| `KC_FALLBACK_BASE_URL` | `http://localhost:11434/v1` | Fallback base URL for OpenAI-compatible API. |
| `KC_HARNESS_API_KEY` | - | API key override for harness. |
| `KC_FALLBACK_API_KEY` | - | Fallback API key. |

## Memory System (`mcp_memory.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `KC_TASK_ID` | - | **Automatically set.** Current task ID for memory provenance tracking. |

## CLI & Task Environment

### Claude Code Integration
| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_USER_PROMPT` | - | User prompt passed to Claude Code (via `mcp_agent_orchestrator.py`). |

## Setting Environment Variables

### Helm Values Configuration
Add environment variables to your `values.yaml`:

```yaml
env:
  - name: ANTHROPIC_API_KEY
    valueFrom:
      secretKeyRef:
        name: claude-secret
        key: apiKey
  - name: TRUSTED_PROXY
    value: "true"
  - name: KC_OPENROUTER_MODEL
    value: "anthropic/claude-3-5-sonnet"
```

### Secrets Management
For sensitive values (API keys, tokens), use Kubernetes Secrets:

```yaml
# secrets/claude.yaml
apiVersion: v1
kind: Secret
metadata:
  name: claude-secret
type: Opaque
data:
  apiKey: BASE64_ENCODED_API_KEY
```

### Development & Testing
For local development, set variables in your shell:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export TRUSTED_PROXY="true"
export READONLY_MODE="false"
python3 charts/workspace/server.py
```

## Security Considerations

1. **`TRUSTED_PROXY`**: Only set to `true` when your ingress proxy properly strips and validates `X-Forwarded-*` and `Remote-User` headers.
2. **`ALLOW_INTERNAL_HOOKS`**: Enable only in single-user/trusted deployments to prevent internal task triggering loops.
3. **API Keys**: Always use Kubernetes Secrets, never hardcode in values files or source code.
4. **`READONLY_MODE`**: Use for demo deployments or when allowing external users to view but not modify workspaces.

## Default Values Reference

Default values are defined in code. To override, set the environment variable to your desired value. Boolean variables accept `true`/`false`, `1`/`0`, `yes`/`no` (case-insensitive).
