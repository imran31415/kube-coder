#!/usr/bin/env python3
# kc-harness — a thin LLM tool-call loop for kube-coder workspaces.
#
# Why this exists: OpenCode + small Ollama models (Qwen 8B-32B, Llama 8B)
# is unreliable. The model emits XML-wrapped tool calls instead of the
# structured `tool_calls` JSON field, OpenCode treats those as plain text,
# and the user gets a *description* of `ls` instead of the actual output.
# A wide tool surface compounds it: 14+ tools, mis-named ones, and small
# models hallucinate (e.g. calling `Read` with `command=ls`).
#
# This script drives the same OpenAI-compatible endpoint with a narrow
# tool set, parses both `tool_calls` AND the XML formats we observed in
# real Ollama responses, and emits JSONL events on stdout that the
# kube-coder dashboard's `formatStreamJsonOutput()` already renders. No
# image rebuild required — ships via the existing browser configmap.
#
# Wiring: server.py's `ClaudeTaskManager.assistant_command()` selects this
# harness when the dashboard task picks the `kc-harness` assistant id.
# The prompt is pasted into the tmux pane via tmux paste-buffer, so we
# read stdin with an idle-timeout terminator (no EOF arrives from paste).
#
# CLI usage (from a workspace terminal):
#   echo "list files in /home/dev" | python3 /tmp/browser/harness.py
#   # or interactively: type the prompt, press Enter, wait for the loop.

import json
import os
import re
import select
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


# ───────────────────────── Config ─────────────────────────

DEFAULT_MODEL = "qwen3:32b-q4_K_M"
MAX_LOOPS = 25
TOOL_OUTPUT_CAP = 64 * 1024  # 64 KiB per tool result
BASH_TIMEOUT = 120
HTTP_TIMEOUT = 180
STDIN_IDLE_SECS = 1.5  # consider prompt complete after this much idle
WORKDIR = os.environ.get("PWD") or os.getcwd()


def pick_model():
    """Per-harness override > shared opencode fallback > hardcoded default.
    Letting both opencode-fallback and kc-harness coexist with different
    models on the same droplet was a deliberate design choice."""
    return (
        os.environ.get("KC_HARNESS_MODEL")
        or os.environ.get("KC_FALLBACK_MODEL")
        or DEFAULT_MODEL
    )


def pick_base_url():
    return os.environ.get("KC_HARNESS_BASE_URL") or os.environ.get(
        "KC_FALLBACK_BASE_URL", "http://localhost:11434/v1"
    )


def pick_api_key():
    return os.environ.get("KC_HARNESS_API_KEY") or os.environ.get(
        "KC_FALLBACK_API_KEY", ""
    )


# ───────────────────────── JSONL events ─────────────────────────
# Shape comes from dashboard.html:formatStreamJsonOutput (parses lines
# with {type:"user"|"assistant"|"result", message.content[], or .result}).
# Keep emit_* writes line-buffered (flush=True) so tmux pipe-pane mirrors
# them to output.log incrementally and the dashboard Output tab updates
# in near real time.

def emit_event(event: dict):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def emit_user_text(text: str):
    emit_event({"type": "user", "message": {"content": [{"type": "text", "text": text}]}})


def emit_assistant_text(text: str):
    emit_event({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def emit_tool_use(name: str, args: dict):
    emit_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": args},
    ]}})


def emit_tool_result(name: str, result: str):
    emit_event({"type": "user", "message": {"content": [
        {"type": "tool_result", "name": name, "content": result},
    ]}})


def emit_final(text: str):
    emit_event({"type": "result", "result": text})


# ───────────────────────── Tool implementations ─────────────────────────
# Narrow on purpose. Each tool's schema is small and unambiguous so a 8-32B
# model can pick the right one. `bash` covers the long tail; the other four
# are conveniences with safer error reporting than running raw shell.

def _truncate(s: str) -> str:
    if len(s) <= TOOL_OUTPUT_CAP:
        return s
    head = s[: TOOL_OUTPUT_CAP - 200]
    return head + f"\n…[truncated {len(s) - len(head)} bytes]"


def tool_bash(args: dict) -> str:
    cmd = args.get("cmd") or args.get("command") or ""
    if not cmd:
        return "ERROR: bash requires a `cmd` argument."
    timeout = int(args.get("timeout") or BASH_TIMEOUT)
    try:
        r = subprocess.run(
            ["bash", "-lc", cmd],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command exceeded timeout of {timeout}s."
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    out = r.stdout
    if r.stderr:
        out = (out + "\n" if out else "") + "[stderr]\n" + r.stderr
    if r.returncode != 0:
        out = (out + "\n" if out else "") + f"[exit {r.returncode}]"
    return _truncate(out or "(no output)")


def tool_read_file(args: dict) -> str:
    path = args.get("path") or args.get("file_path") or ""
    if not path:
        return "ERROR: read_file requires a `path` argument."
    try:
        return _truncate(Path(path).read_text())
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def tool_write_file(args: dict) -> str:
    path = args.get("path") or args.get("file_path") or ""
    content = args.get("content", "")
    if not path:
        return "ERROR: write_file requires a `path` argument."
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"OK: wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def tool_list_dir(args: dict) -> str:
    path = args.get("path") or "."
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except FileNotFoundError:
        return f"ERROR: directory not found: {path}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    lines = []
    for e in entries:
        tag = "d" if e.is_dir(follow_symlinks=False) else ("l" if e.is_symlink() else "-")
        lines.append(f"{tag} {e.name}")
    return _truncate("\n".join(lines) or "(empty)")


def tool_edit_file(args: dict) -> str:
    path = args.get("path") or args.get("file_path") or ""
    find = args.get("find", "")
    replace = args.get("replace", "")
    if not path or not find:
        return "ERROR: edit_file requires `path` and `find` arguments."
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    count = text.count(find)
    if count == 0:
        return f"ERROR: `find` string not present in {path}."
    if count > 1:
        return (
            f"ERROR: `find` string occurs {count} times in {path}; "
            "expand it to match exactly one occurrence."
        )
    new_text = text.replace(find, replace, 1)
    Path(path).write_text(new_text)
    return f"OK: replaced one occurrence in {path}"


TOOLS = {
    "bash": (tool_bash, {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the workspace. Returns combined stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Shell command to run."},
                    "timeout": {"type": "integer", "description": "Optional seconds, default 120."},
                },
                "required": ["cmd"],
            },
        },
    }),
    "read_file": (tool_read_file, {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }),
    "write_file": (tool_write_file, {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content. Parent dirs are created.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    }),
    "list_dir": (tool_list_dir, {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries in a directory (one per line, prefixed d/-/l).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }),
    "edit_file": (tool_edit_file, {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace exactly one occurrence of `find` with `replace` in a file. "
                "Errors if the find string is missing or appears more than once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                },
                "required": ["path", "find", "replace"],
            },
        },
    }),
}


def tools_schema():
    return [defn for (_, defn) in TOOLS.values()]


def execute_tool(name: str, args: dict) -> str:
    entry = TOOLS.get(name)
    if not entry:
        return f"ERROR: unknown tool `{name}`. Valid: {', '.join(TOOLS)}."
    fn, _ = entry
    try:
        return fn(args or {})
    except Exception as e:
        return f"ERROR: tool `{name}` raised {type(e).__name__}: {e}"


# ───────────────────────── XML fallback parser ─────────────────────────
# Real Ollama responses observed in the kc-harness design session:
#
#   <function=bash>
#     <parameter=cmd>ls -la</parameter>
#   </function>
#   </tool_call>          ← qwen3-coder style
#
#   <tool_call>{"name":"bash","arguments":{"cmd":"ls -la"}}</tool_call>
#                          ← Hermes JSON style
#
# OpenCode parses neither; we try both before giving up.

_FUNC_RE = re.compile(r"<function=(?P<name>[A-Za-z_][\w-]*)>(?P<body>.*?)</function>", re.DOTALL)
_PARAM_RE = re.compile(r"<parameter=(?P<key>[A-Za-z_][\w-]*)>(?P<val>.*?)</parameter>", re.DOTALL)
_TOOL_CALL_JSON_RE = re.compile(r"<tool_call>\s*(?P<body>\{.*?\})\s*</tool_call>", re.DOTALL)


def _coerce_arg(raw: str):
    """Try to turn a string param value into a typed JSON value when sensible."""
    s = raw.strip()
    if not s:
        return ""
    # numeric
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    # bool / null
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    # JSON object or array
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            return json.loads(s)
        except Exception:
            pass
    return s  # plain string


def parse_xml_tool_calls(content: str):
    calls = []
    # Hermes JSON-in-XML form first.
    for m in _TOOL_CALL_JSON_RE.finditer(content):
        try:
            j = json.loads(m.group("body"))
            name = j.get("name")
            args = j.get("arguments") or j.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            if name:
                calls.append({"name": name, "arguments": args})
        except Exception:
            continue
    # Function/parameter form.
    for m in _FUNC_RE.finditer(content):
        name = m.group("name")
        body = m.group("body")
        args = {}
        for pm in _PARAM_RE.finditer(body):
            args[pm.group("key")] = _coerce_arg(pm.group("val"))
        # if there are no <parameter=…> tags but the body has text, treat
        # the whole body as the single positional `cmd` for the bash tool.
        if not args and body.strip() and name == "bash":
            args["cmd"] = body.strip()
        if name:
            calls.append({"name": name, "arguments": args})
    return calls


# ───────────────────────── HTTP to the LLM ─────────────────────────

def chat(messages, base_url, api_key, model):
    body = json.dumps({
        "model": model,
        "messages": messages,
        "tools": tools_schema(),
        "tool_choice": "auto",
        "stream": False,
        "temperature": 0.1,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body, headers=headers,
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


# ───────────────────────── Read prompt from stdin ─────────────────────────
# tmux paste-buffer types the prompt into the terminal without sending EOF,
# so we can't `sys.stdin.read()`. Instead read in chunks and treat
# STDIN_IDLE_SECS of silence as the end of input. Works for both interactive
# typing and dashboard paste delivery. If stdin is closed (CLI piping),
# os.read returns b"" and we surface that to the caller as None so REPL
# mode can exit cleanly.

def read_prompt(first_chunk_timeout=None):
    """Read one prompt's worth of input. Returns the text, "" on idle with
    no input, or None on EOF (caller should exit).

    `first_chunk_timeout`: how long to wait for the first byte. None means
    wait forever (REPL between prompts). 10s on the first prompt so a stuck
    tmux paste doesn't hang the pod startup forever."""
    fd = sys.stdin.fileno()
    buf = []
    started = False
    deadline = (time.time() + first_chunk_timeout) if first_chunk_timeout else None
    while True:
        if started:
            timeout = STDIN_IDLE_SECS
        elif deadline is not None:
            remaining = deadline - time.time()
            if remaining <= 0:
                return ""  # nothing arrived within first_chunk_timeout
            timeout = remaining
        else:
            timeout = None  # block until first chunk
        rlist, _, _ = select.select([fd], [], [], timeout)
        if not rlist:
            if started:
                break  # idle after some input → submit
            continue
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            return None
        if not chunk:  # EOF — caller should exit the REPL
            return None if not started else "".join(buf).strip()
        buf.append(chunk.decode("utf-8", "replace"))
        started = True
    return "".join(buf).strip()


# ───────────────────────── Main loop ─────────────────────────

SYSTEM_PROMPT = (
    "You are kc-harness, a workspace assistant running on a kube-coder pod.\n"
    f"Current working directory: {WORKDIR}\n"
    "\n"
    "Rules:\n"
    "1. To act on the workspace, call a tool. Do not just describe what a "
    "command would do — call `bash` with the command itself.\n"
    "2. Prefer the most specific tool: `list_dir` to list a directory, "
    "`read_file` to read, `write_file` to create/replace, `edit_file` for "
    "a single substring substitution, `bash` for everything else.\n"
    "3. Emit tool calls in the structured `tool_calls` field. Do NOT wrap "
    "them in <tool_call>, <function=…>, or any other XML.\n"
    "4. After a tool result, decide: call another tool, or answer the user. "
    "Stop calling tools as soon as you have enough information to answer.\n"
)


def run_once(prompt: str, base_url: str, api_key: str, model: str, messages=None):
    """Process one user turn. If `messages` is provided, append to it (REPL
    mode); otherwise start a fresh thread with the system prompt. Returns
    the updated messages list so the caller can carry context forward."""
    if messages is None:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({"role": "user", "content": prompt})
    emit_user_text(prompt)

    for step in range(MAX_LOOPS):
        try:
            resp = chat(messages, base_url, api_key, model)
        except urllib.error.URLError as e:
            emit_final(f"upstream API error: {e}")
            return messages
        except Exception as e:
            emit_final(f"unexpected error talking to model: {type(e).__name__}: {e}")
            return messages

        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            emit_final(f"malformed model response: {json.dumps(resp)[:300]}")
            return messages

        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""

        # XML fallback when the model emitted tool calls as text.
        if not tool_calls and content:
            xml_calls = parse_xml_tool_calls(content)
            if xml_calls:
                tool_calls = [
                    {
                        "id": f"xml-{step}-{i}",
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"]),
                        },
                    }
                    for i, c in enumerate(xml_calls)
                ]

        if not tool_calls:
            # No tool call → treat content as the final answer.
            emit_assistant_text(content)
            emit_final(content)
            messages.append({"role": "assistant", "content": content})
            return messages

        # Replay the assistant turn in the message history. Use the
        # canonical `tool_calls` form so the server understands the
        # subsequent `tool` role messages.
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or "(unknown)"
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except Exception:
                args = {"_raw_arguments": raw_args}

            emit_tool_use(name, args)
            result = execute_tool(name, args)
            emit_tool_result(name, result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or f"call-{step}",
                "name": name,
                "content": result,
            })

    emit_final(f"(stopped after {MAX_LOOPS} tool loops; the model never produced a final answer)")
    return messages


REPL_BANNER_BOTTOM = (
    "\n"
    "─── kc-harness ────────────────────────────────────────────────────────\n"
    "Type or paste your next prompt then wait. Ctrl-D / type `/exit` to quit.\n"
    "──────────────────────────────────────────────────────────────────────\n"
)


def main():
    base_url = pick_base_url()
    api_key = pick_api_key()
    model = pick_model()

    # CLI flags. --once <prompt> runs a single prompt and exits (used by the
    # test harness). Without --once, run as an interactive REPL so the tmux
    # pane stays alive and the user can ask follow-ups via the Chat tab.
    argv = sys.argv[1:]
    one_shot_prompt = None
    while argv:
        a = argv.pop(0)
        if a == "--model" and argv:
            model = argv.pop(0)
        elif a == "--once" and argv:
            one_shot_prompt = argv.pop(0)
        elif a in ("-h", "--help"):
            sys.stderr.write(__doc__ or "kc-harness — see source for usage.\n")
            return
        else:
            sys.stderr.write(f"kc-harness: unknown arg {a!r}\n")
            return

    sys.stderr.write(f"kc-harness → {base_url} model={model}\n")
    sys.stderr.flush()

    if one_shot_prompt is not None:
        run_once(one_shot_prompt, base_url, api_key, model)
        return

    # ─── REPL ───
    # First prompt has a generous deadline (dashboard pastes via tmux
    # paste-buffer ~3s after pod-spawn). Subsequent prompts block forever
    # so the tmux pane stays alive until the user closes it.
    messages = None
    first = True
    while True:
        prompt = read_prompt(first_chunk_timeout=300 if first else None)
        first = False
        if prompt is None:
            sys.stderr.write("kc-harness: stdin closed, exiting.\n")
            return
        if not prompt:
            # Idle timeout with no input at all on the first read — bail
            # rather than blocking forever on a tmux session whose paste
            # never landed (would otherwise look hung from the dashboard).
            emit_final("(no prompt received)")
            return
        if prompt.strip().lower() in ("/exit", "/quit", "exit", "quit", ":q"):
            sys.stderr.write("kc-harness: bye.\n")
            return
        messages = run_once(prompt, base_url, api_key, model, messages=messages)
        sys.stderr.write(REPL_BANNER_BOTTOM)
        sys.stderr.flush()


if __name__ == "__main__":
    main()
