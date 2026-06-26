# LLM setup

How the three assistants are wired up, what each one is for, and how to
configure the GPU droplet for the open-source path.

## The three assistants

Each kube-coder workspace exposes up to three assistant options in the
dashboard's New Task dropdown. All three share the same task lifecycle
(tmux session, JSONL output, memory injection, dashboard rendering); they
differ only in which CLI/process runs inside the pane and which model
backs it.

| Id                     | Label             | Backed by                              | Use it for                                          |
| ---------------------- | ----------------- | -------------------------------------- | --------------------------------------------------- |
| `claude`               | Claude Code       | Anthropic API (`claude` CLI in pod)    | Default. Full agentic work.                         |
| `gemini`               | Google Gemini     | Gemini API (`gemini` CLI in pod)       | Native Google models; generous free tier.           |
| `opencode-openrouter`  | OpenRouter        | `opencode` CLI → OpenRouter            | Claude-class behaviour without Anthropic billing.   |
| `kc-harness`           | Opensource GPU    | In-pod Python loop → Ollama (or any OpenAI-compatible endpoint) | Self-hosted open models on a GPU droplet.           |

Claude Code is always available. The other two only appear in the
dropdown when their config is present (see *Enabling* below).

## Architecture

```
                Dashboard                  Workspace pod
                ─────────                  ─────────────
   New Task   ──assistant=claude──>  tmux: claude ────────────────> Anthropic API
              ──assistant=openrouter> tmux: opencode --model … ───> OpenRouter
              ──assistant=kc-harness> tmux: python3 harness.py ──> Ollama / vLLM /
                                                                    any OpenAI-
                                                                    compatible :11434
```

Selection is per-task (recorded on the task, survives reload). The
assistant choice is mapped to a shell command by
`ClaudeTaskManager.assistant_command()` in `charts/workspace/server.py`,
and that command is what tmux starts in the new pane.

## Claude Code

Path of least friction. Two auth modes:

- **OAuth** — leave `claude.apiKey` empty in values, run `claude` once in
  the pod and complete the device-code flow. Tokens persist on the PVC.
- **API key** — set `claude.apiKey` in `secrets/<user>/claude.yaml`. Wins
  over OAuth when set.

No further config needed; the `claude` CLI ships in the image.

## OpenRouter

Runs OpenCode inside the pane, pointed at OpenRouter. The dashboard
surfaces it as the `OpenRouter` option only when an OpenRouter key is
configured.

```yaml
# secrets/<user>/assistant.yaml (gitignored)
assistant:
  openrouter:
    apiKey: "sk-or-v1-…"                # https://openrouter.ai/keys
    model: "anthropic/claude-sonnet-4"  # any OpenRouter model slug
```

Wiring:

- Helm renders `OPENROUTER_API_KEY` and `KC_OPENROUTER_MODEL` env vars on
  the pod (see `charts/workspace/templates/deployment.yaml`).
- The entrypoint writes `~/.config/opencode/opencode.json`. OpenCode
  auto-discovers OpenRouter from `OPENROUTER_API_KEY` — we deliberately
  don't emit a stub provider block for it, because any incomplete
  first-class entry alongside a custom provider makes OpenCode's config
  loader silently drop the rest.
- `assistant_command('opencode-openrouter')` returns
  `opencode --model openrouter/<model>`.

## Google Gemini

Runs Google's open-source `gemini` CLI inside the pane against the native
Gemini API. The dashboard surfaces it as the `Google Gemini` option only
when a Gemini API key is configured.

```yaml
# secrets/<user>/assistant.yaml (gitignored)
assistant:
  gemini:
    apiKey: "AIza…"          # https://aistudio.google.com/apikey
    model: "gemini-2.5-pro"  # or gemini-2.5-flash
```

Wiring:

- Helm renders `GEMINI_API_KEY` (from the assistant secret) and
  `KC_GEMINI_MODEL` env vars on the pod (see
  `charts/workspace/templates/deployment.yaml`).
- The `gemini` CLI is npm-installed in the image (`@google/gemini-cli`),
  so there's no on-disk config to write — it auto-discovers the key from
  `GEMINI_API_KEY`.
- `assistant_command('gemini')` returns `gemini -m <model>` (interactive
  dashboard REPL). The agent-orchestrator's headless path runs
  `gemini --yolo -m <model> -p <prompt>` so spawned sub-agents complete
  unattended and report an exit code.

## Opensource GPU (`kc-harness`)

Talks to any OpenAI-compatible endpoint (typically Ollama on a private
GPU droplet) via a narrow in-pod Python tool-call loop. Defined in
`charts/workspace/harness.py`, shipped to the pod via the browser
configmap and executed at `/tmp/browser/harness.py`.

### Why it isn't OpenCode

OpenCode's tool-call protocol assumes the model emits the OpenAI
`tool_calls` JSON field. Small Ollama models (Qwen 8B–32B, Llama 8B,
Qwen3-Coder via Ollama's template) routinely emit XML-wrapped calls
instead:

```
<function=bash>
  <parameter=cmd>ls -la</parameter>
</function>
```

or the Hermes-style

```
<tool_call>{"name":"bash","arguments":{"cmd":"ls -la"}}</tool_call>
```

OpenCode parses neither, treats them as plain text, and the user sees a
*description* of what `ls` would do instead of the output. OpenCode also
advertises ~14 tools with a long system prompt that small models refuse
to use ("I don't have access to execute shell commands"). `kc-harness`
fixes both: a narrow tool surface and a fallback XML parser.

### Five tools, on purpose

| Tool         | Args                              | Notes                                                 |
| ------------ | --------------------------------- | ----------------------------------------------------- |
| `bash`       | `cmd`, optional `timeout`         | Shell command in the workspace cwd. Captures stdout+stderr, caps at 64 KiB. |
| `read_file`  | `path`                            | Returns file contents.                                |
| `write_file` | `path`, `content`                 | Creates or overwrites; mkdir -p the parent.           |
| `list_dir`   | `path` (default `.`)              | One entry per line, prefixed `d`/`-`/`l`.             |
| `edit_file`  | `path`, `find`, `replace`         | Errors if `find` is missing or appears more than once. |

`bash` covers the long tail; the other four exist so a small model can
pick an unambiguous tool instead of misnaming `read` as `command=ls`.

### Tool-call parsing

`run_once()` reads `choices[0].message.tool_calls` first. If the field
is empty but `content` contains XML in either of the formats above,
`parse_xml_tool_calls()` extracts the call and synthesises the canonical
`tool_calls` shape so subsequent `tool`-role messages reference it
correctly. The loop stops as soon as the model produces a turn with no
tool call (final answer) or after `MAX_LOOPS=25`.

### Output protocol

Each turn emits two streams on stdout:

1. **JSONL events** in the dashboard's `formatStreamJsonOutput` shape
   (`{type:"user"|"assistant"|"result", message.content[]}`). These
   populate the Output tab.
2. **ANSI-coloured pretty lines** for the live tmux pane. These never
   start with `{` so the JSONL parser ignores them. They make the Chat
   tab look like a TUI instead of a JSON dump.

### Configuration

Same Helm values block as OpenCode's fallback; `kc-harness` is the
consumer.

```yaml
# secrets/<user>/assistant.yaml (gitignored)
assistant:
  fallback:
    baseUrl: "http://<droplet-ip>:11434/v1"  # any OpenAI-compatible URL
    apiKey: ""                                # optional bearer token
    model: "qwen3-coder:30b"                  # the Ollama tag
    providerId: "ollama-droplet"              # cosmetic
    providerName: "Ollama (GPU droplet)"      # cosmetic
```

Env vars rendered on the pod (`charts/workspace/templates/deployment.yaml`):

| Env var                     | Purpose                                                       |
| --------------------------- | ------------------------------------------------------------- |
| `KC_FALLBACK_BASE_URL`      | Endpoint kc-harness POSTs to (`<baseUrl>/chat/completions`).  |
| `KC_FALLBACK_API_KEY`       | Sent as `Authorization: Bearer …` when set.                   |
| `KC_FALLBACK_MODEL`         | Default model. Overridable per-pod by `KC_HARNESS_MODEL`.     |
| `KC_FALLBACK_PROVIDER_ID`   | Dashboard label only.                                         |
| `KC_FALLBACK_PROVIDER_NAME` | Dashboard label only.                                         |

`KC_HARNESS_*` env vars (`KC_HARNESS_BASE_URL`, `KC_HARNESS_MODEL`,
`KC_HARNESS_API_KEY`) win over the `KC_FALLBACK_*` equivalents when set
— useful if you want kc-harness to point at a different model than other
clients sharing the same endpoint.

## GPU droplet setup (the Ollama side)

The droplet runs Ollama on `:11434` bound to `0.0.0.0`. Two systemd
overrides are required for tool-calling to work end to end:

```
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\nEnvironment="OLLAMA_NUM_CTX=32768"\nEnvironment="OLLAMA_KEEP_ALIVE=30m"\n' \
  | sudo tee /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

- `OLLAMA_HOST=0.0.0.0:11434` — required after fresh installs (default
  is 127.0.0.1).
- `OLLAMA_NUM_CTX=32768` — Ollama's default 2048-token context is smaller
  than OpenCode/kc-harness's system prompt + tool schemas combined; with
  the default, tool definitions get truncated and the model loses access
  to its own tools.
- `OLLAMA_KEEP_ALIVE=30m` — keeps the model resident in VRAM between
  requests so the second prompt doesn't pay another 30–60 s reload.

Pull whatever model you want kc-harness to use:

```
ollama pull qwen3-coder:30b
```

The qwen3-coder family is what kc-harness's XML fallback was designed
around; anything else that can tool-call (qwen3:32b, llama3.1:8b) also
works, but pick a coder-tuned model for code tasks.

## Enabling on a workspace

Both alternate assistants are independent. Set one, the other, or both.

```
# 1. drop credentials in a gitignored secrets file
cp templates/assistant-secrets-template.yaml secrets/<user>/assistant.yaml
$EDITOR secrets/<user>/assistant.yaml

# 2. deploy
make deploy USER=<user>
```

`make deploy` auto-includes every `secrets/<user>/*.yaml` (and for the
private set, `users-private/<user>/secrets/*.yaml`). On pod start, the
entrypoint renders `~/.config/opencode/opencode.json` if either
OpenRouter or fallback creds are present, and the env vars described
above land on the container.

## Per-task selection

### Dashboard

The New Task form shows an assistant dropdown whenever more than one is
available. The selection is recorded on the task and shown on the
Claude Tasks panel so you can see at a glance which assistant ran each.

### HTTP API

```
POST /api/claude/tasks
Authorization: Bearer $TOKEN

{
  "prompt": "Refactor the auth middleware",
  "assistant": "kc-harness"          // or "claude" or "opencode-openrouter"
}
```

Unknown or disabled values fall back to `claude` rather than failing the
request.

### What surfaces to the dashboard

`GET /api/claude/assistants` reports the enabled set:

```json
{
  "default": "claude",
  "assistants": [
    {"id": "claude", "label": "Claude Code", "default": true},
    {"id": "opencode-openrouter", "label": "OpenRouter", "model": "anthropic/claude-sonnet-4"},
    {"id": "kc-harness", "label": "Opensource GPU", "model": "qwen3-coder:30b"}
  ]
}
```

## File map

| File                                                        | Role                                                              |
| ----------------------------------------------------------- | ----------------------------------------------------------------- |
| `charts/workspace/server.py`                                | `ClaudeTaskManager.ASSISTANTS`, `assistant_command()`, `create_task()`. |
| `charts/workspace/harness.py`                               | kc-harness implementation (tools, XML parser, REPL).              |
| `charts/workspace/harness_test.py`                          | Unit tests for parsing / tool execution.                          |
| `charts/workspace/templates/browser-configmap.yaml`         | Packages `harness.py` into the configmap mounted at `/tmp/browser`. |
| `charts/workspace/templates/workspace-entrypoint-configmap.yaml` | Renders `~/.config/opencode/opencode.json` when fallback or openrouter is set. |
| `charts/workspace/templates/deployment.yaml`                | Maps Helm values → `KC_FALLBACK_*` / `OPENROUTER_*` env vars.    |
| `charts/workspace/values.yaml`                              | Defaults; everything empty so the public-repo ships Claude-only. |
| `secrets/<user>/assistant.yaml`                             | Per-user credentials (gitignored).                                |
| `templates/assistant-secrets-template.yaml`                 | Starter for the above.                                            |
