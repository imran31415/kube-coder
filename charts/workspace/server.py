#!/usr/bin/env python3
import http.server
import html
import subprocess
import os
import sys
import json
import time
import re
import base64
import collections
import hmac
import hashlib
import secrets
import shutil
import threading
import uuid
import urllib.parse
import urllib.request
import http.client
import fcntl

# Persistent memory subsystem — shared with mcp_memory.py via the colocated
# `memory` package. Importable because the workspace-entrypoint copies the
# package next to server.py at /tmp/browser/.
try:
    from memory.manager import (
        MemoryManager,
        MemoryError as MemError,
        NotFound as MemNotFound,
        Conflict as MemConflict,
        ValidationError as MemValidationError,
    )
    from memory.sync import ClaudeMemorySyncer
    _MEMORY_AVAILABLE = True
except Exception as _mem_import_err:  # broken install shouldn't crash the server
    MemoryManager = None  # type: ignore
    ClaudeMemorySyncer = None  # type: ignore
    MemError = MemNotFound = MemConflict = MemValidationError = Exception  # type: ignore
    _MEMORY_AVAILABLE = False
    print(f'[memory] import failed: {_mem_import_err}', file=sys.stderr)

# Alert thresholds for metrics
ALERT_THRESHOLDS = {
    'cpu': {'warning': 70, 'critical': 90},
    'memory': {'warning': 80, 'critical': 95},
    'disk': {'warning': 80, 'critical': 90}
}

# Public-demo / read-only mode. Set from helm values via env vars on the
# pod spec. READONLY_MODE gates every POST/DELETE/PUT in BrowserHandler;
# AUTH_MODE='none' short-circuits check_claude_auth for deployments without
# an oauth2-proxy in front. _check_safety_invariants() below refuses to
# start if AUTH_MODE=none without READONLY_MODE=true — no unauthed writes.
READONLY_MODE = os.environ.get('READONLY_MODE', 'false').lower() == 'true'
AUTH_MODE = os.environ.get('AUTH_MODE', 'basic').lower()
# DEMO_SHOW_ALL=true makes the SPA *render* every mutation control instead of
# hiding it (MutatorOnly), so the public demo shows the full UI surface — but
# the server still 403s every write via _readonly_block. Presentation-only
# hint surfaced through /api/mode; it does NOT relax any gate. Only meaningful
# alongside READONLY_MODE=true (the demo deploy); inert otherwise.
DEMO_SHOW_ALL = os.environ.get('DEMO_SHOW_ALL', 'false').lower() == 'true'
# TRUSTED_PROXY=true tells check_claude_auth it's safe to honor
# X-Auth-Request-User / X-Auth-Request-Email / Remote-User headers from the
# request. Without it we ignore those headers — the only ways to authenticate
# become AUTH_MODE=none (gated to readonly) or a Bearer token. Set to true
# when an upstream proxy strips client-supplied auth headers (e.g. our
# oauth2-proxy + ingress).
TRUSTED_PROXY = os.environ.get('TRUSTED_PROXY', 'true').lower() == 'true'
# Hard cap on JSON request bodies. Without this, a single
# Content-Length: huge POST will allocate the body before parsing and OOM
# the pod. Override via MAX_REQUEST_BODY_BYTES.
MAX_REQUEST_BODY_BYTES = int(os.environ.get('MAX_REQUEST_BODY_BYTES', str(1024 * 1024)))
# Hard ceiling on /stream connection lifetime. Clients are expected to
# reconnect; without this an unbounded handler-thread leak is the path of
# least resistance to DoS. Override via STREAM_MAX_SECONDS.
STREAM_MAX_SECONDS = int(os.environ.get('STREAM_MAX_SECONDS', '1800'))
# SSRF guard for the completion-hook response_url. By default we refuse to
# POST to RFC1918 / link-local / loopback so a malicious caller cannot turn
# us into a probe of the cloud metadata service or in-cluster services.
# Set ALLOW_INTERNAL_HOOKS=true to opt back in (single-user trusted deploy).
ALLOW_INTERNAL_HOOKS = os.environ.get('ALLOW_INTERNAL_HOOKS', 'false').lower() == 'true'

def _check_safety_invariants():
    if AUTH_MODE == 'none' and not READONLY_MODE:
        print(
            '[server.py] FATAL: AUTH_MODE=none requires READONLY_MODE=true. '
            'Refusing to start an unauthed, writable workspace.',
            file=sys.stderr,
        )
        sys.exit(2)
    if READONLY_MODE:
        print('[server.py] READONLY_MODE active — mutating endpoints will 403.', file=sys.stderr)
    if AUTH_MODE == 'none':
        print('[server.py] AUTH_MODE=none — check_claude_auth short-circuits to True.', file=sys.stderr)
    if DEMO_SHOW_ALL:
        print('[server.py] DEMO_SHOW_ALL=true — SPA renders mutation UI (still 403-gated).', file=sys.stderr)
_check_safety_invariants()

# Strip ANSI escape sequences (CSI, OSC, single-char) from terminal output
# captured via `tmux pipe-pane`, so the dashboard chat view stays readable.
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;?]*[ -/]*[@-~]'   # CSI
    r'|\x1b\][^\x07]*\x07'          # OSC ... BEL
    r'|\x1b[NOPYZ\\^_=>78<]'        # single-char escapes
)


def strip_ansi(text):
    return _ANSI_RE.sub('', text)


# How long a task's rendered tmux screen must stay unchanged before we treat
# it as waiting-for-input. While an agent works it streams output / animates a
# spinner+timer, so the screen keeps changing; a static screen means it has
# finished its turn (or hit a prompt) and is awaiting the human. This replaced
# a regex scraper that never worked against the agents' full-screen TUIs.
# Env-overridable for tuning.
try:
    IDLE_WAITING_SECONDS = float(os.environ.get('KC_IDLE_WAITING_SECONDS', '90'))
except ValueError:
    IDLE_WAITING_SECONDS = 90.0

class MetricsCollector:
    """Collects system metrics from /proc filesystem and os.statvfs"""

    @staticmethod
    def get_cpu_usage():
        """Get CPU usage percentage using /proc/stat"""
        try:
            def read_cpu_times():
                with open('/proc/stat', 'r') as f:
                    line = f.readline()
                    parts = line.split()
                    # cpu user nice system idle iowait irq softirq steal guest guest_nice
                    if parts[0] == 'cpu':
                        times = [int(x) for x in parts[1:]]
                        idle = times[3] + times[4]  # idle + iowait
                        total = sum(times)
                        return idle, total
                return 0, 0

            idle1, total1 = read_cpu_times()
            time.sleep(0.5)
            idle2, total2 = read_cpu_times()

            idle_delta = idle2 - idle1
            total_delta = total2 - total1

            if total_delta == 0:
                usage_percent = 0.0
            else:
                usage_percent = ((total_delta - idle_delta) / total_delta) * 100

            # Count CPU cores
            cores = 0
            with open('/proc/stat', 'r') as f:
                for line in f:
                    if line.startswith('cpu') and line[3].isdigit():
                        cores += 1

            return {
                'usage_percent': round(usage_percent, 1),
                'cores': cores if cores > 0 else 1
            }
        except Exception as e:
            return {'usage_percent': 0.0, 'cores': 1, 'error': str(e)}

    @staticmethod
    def get_memory_usage():
        """Get memory usage from /proc/meminfo"""
        try:
            meminfo = {}
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    key = parts[0].rstrip(':')
                    value = int(parts[1])  # Value in kB
                    meminfo[key] = value

            total_kb = meminfo.get('MemTotal', 0)
            available_kb = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
            used_kb = total_kb - available_kb

            total_mb = total_kb / 1024
            used_mb = used_kb / 1024
            available_mb = available_kb / 1024

            percent = (used_kb / total_kb * 100) if total_kb > 0 else 0

            return {
                'total_mb': round(total_mb, 1),
                'used_mb': round(used_mb, 1),
                'available_mb': round(available_mb, 1),
                'percent': round(percent, 1)
            }
        except Exception as e:
            return {'total_mb': 0, 'used_mb': 0, 'available_mb': 0, 'percent': 0, 'error': str(e)}

    @staticmethod
    def get_disk_usage():
        """Get disk usage for /home/dev"""
        try:
            path = '/home/dev'
            if not os.path.exists(path):
                path = '/'

            stat = os.statvfs(path)
            total_bytes = stat.f_blocks * stat.f_frsize
            available_bytes = stat.f_bavail * stat.f_frsize
            used_bytes = total_bytes - available_bytes

            total_gb = total_bytes / (1024 ** 3)
            used_gb = used_bytes / (1024 ** 3)
            available_gb = available_bytes / (1024 ** 3)

            percent = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0

            return {
                'total_gb': round(total_gb, 1),
                'used_gb': round(used_gb, 1),
                'available_gb': round(available_gb, 1),
                'percent': round(percent, 1),
                'path': path
            }
        except Exception as e:
            return {'total_gb': 0, 'used_gb': 0, 'available_gb': 0, 'percent': 0, 'path': '/home/dev', 'error': str(e)}

    @staticmethod
    def get_alerts(cpu, memory, disk):
        """Generate alerts based on current metrics"""
        alerts = []

        if cpu.get('usage_percent', 0) >= ALERT_THRESHOLDS['cpu']['critical']:
            alerts.append({'type': 'critical', 'resource': 'cpu', 'message': f"CPU usage at {cpu['usage_percent']}%"})
        elif cpu.get('usage_percent', 0) >= ALERT_THRESHOLDS['cpu']['warning']:
            alerts.append({'type': 'warning', 'resource': 'cpu', 'message': f"CPU usage at {cpu['usage_percent']}%"})

        if memory.get('percent', 0) >= ALERT_THRESHOLDS['memory']['critical']:
            alerts.append({'type': 'critical', 'resource': 'memory', 'message': f"Memory usage at {memory['percent']}%"})
        elif memory.get('percent', 0) >= ALERT_THRESHOLDS['memory']['warning']:
            alerts.append({'type': 'warning', 'resource': 'memory', 'message': f"Memory usage at {memory['percent']}%"})

        if disk.get('percent', 0) >= ALERT_THRESHOLDS['disk']['critical']:
            alerts.append({'type': 'critical', 'resource': 'disk', 'message': f"Disk usage at {disk['percent']}%"})
        elif disk.get('percent', 0) >= ALERT_THRESHOLDS['disk']['warning']:
            alerts.append({'type': 'warning', 'resource': 'disk', 'message': f"Disk usage at {disk['percent']}%"})

        return alerts

    @staticmethod
    def get_all_metrics():
        """Return all metrics as a dictionary"""
        cpu = MetricsCollector.get_cpu_usage()
        memory = MetricsCollector.get_memory_usage()
        disk = MetricsCollector.get_disk_usage()
        alerts = MetricsCollector.get_alerts(cpu, memory, disk)

        return {
            'cpu': cpu,
            'memory': memory,
            'disk': disk,
            'alerts': alerts,
            'timestamp': time.time()
        }


class GitHubManager:
    """Handles GitHub authentication and configuration"""

    SSH_DIR = os.path.expanduser('~/.ssh')
    GH_CONFIG_DIR = os.path.expanduser('~/.config/gh')

    @staticmethod
    def get_ssh_status():
        """Check if SSH key exists and get its details"""
        key_path = os.path.join(GitHubManager.SSH_DIR, 'id_ed25519')
        pub_key_path = key_path + '.pub'

        if not os.path.exists(pub_key_path):
            return {'configured': False}

        try:
            with open(pub_key_path, 'r') as f:
                public_key = f.read().strip()

            # Get fingerprint
            result = subprocess.run(
                ['ssh-keygen', '-lf', pub_key_path],
                capture_output=True, text=True
            )
            fingerprint = result.stdout.split()[1] if result.returncode == 0 else 'unknown'

            return {
                'configured': True,
                'key_type': 'ed25519',
                'key_fingerprint': fingerprint,
                'public_key': public_key
            }
        except Exception as e:
            return {'configured': False, 'error': str(e)}

    @staticmethod
    def generate_ssh_key(email):
        """Generate new SSH key pair"""
        key_path = os.path.join(GitHubManager.SSH_DIR, 'id_ed25519')
        os.makedirs(GitHubManager.SSH_DIR, mode=0o700, exist_ok=True)

        # Remove existing key if present
        for ext in ['', '.pub']:
            path = key_path + ext
            if os.path.exists(path):
                os.remove(path)

        result = subprocess.run([
            'ssh-keygen', '-t', 'ed25519', '-C', email,
            '-f', key_path, '-N', ''
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise Exception(f"Failed to generate key: {result.stderr}")

        # Add GitHub config to SSH config file
        config_path = os.path.join(GitHubManager.SSH_DIR, 'config')
        github_config = """
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
"""
        # Check if config exists and already has github.com
        existing_config = ''
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                existing_config = f.read()

        if 'github.com' not in existing_config:
            with open(config_path, 'a') as f:
                f.write(github_config)
            os.chmod(config_path, 0o600)

        return GitHubManager.get_ssh_status()

    @staticmethod
    def get_gh_cli_status():
        """Check gh CLI authentication status"""
        try:
            result = subprocess.run(
                ['gh', 'auth', 'status', '--hostname', 'github.com'],
                capture_output=True, text=True
            )

            if result.returncode != 0:
                return {'installed': True, 'authenticated': False}

            # Parse output to get username (gh writes to stderr)
            output = result.stderr + result.stdout
            username = None
            for line in output.split('\n'):
                if 'Logged in to github.com' in line:
                    # Try to extract username
                    if 'account' in line:
                        parts = line.split('account')
                        if len(parts) > 1:
                            username = parts[1].strip().split()[0].strip('()')
                    break

            return {
                'installed': True,
                'authenticated': True,
                'username': username
            }
        except FileNotFoundError:
            return {'installed': False, 'authenticated': False}
        except Exception as e:
            return {'installed': True, 'authenticated': False, 'error': str(e)}

    @staticmethod
    def start_device_flow():
        """Start gh auth device flow - returns instructions for manual auth"""
        # We can't truly start interactive device flow from a server
        # Instead, provide instructions for the user
        return {
            'instructions': 'Run the following command in the terminal to authenticate:',
            'command': 'gh auth login --hostname github.com --git-protocol https --web',
            'manual_steps': [
                '1. Open Terminal from the dashboard',
                '2. Run: gh auth login',
                '3. Select GitHub.com',
                '4. Select HTTPS',
                '5. Authenticate with browser when prompted',
                '6. Return here and click "Check Status"'
            ]
        }

    @staticmethod
    def get_git_config():
        """Get git global config"""
        try:
            name_result = subprocess.run(
                ['git', 'config', '--global', 'user.name'],
                capture_output=True, text=True
            )
            email_result = subprocess.run(
                ['git', 'config', '--global', 'user.email'],
                capture_output=True, text=True
            )
            return {
                'user_name': name_result.stdout.strip() if name_result.returncode == 0 else '',
                'user_email': email_result.stdout.strip() if email_result.returncode == 0 else ''
            }
        except Exception as e:
            return {'user_name': '', 'user_email': '', 'error': str(e)}

    @staticmethod
    def set_git_config(name, email):
        """Set git global config"""
        try:
            subprocess.run(['git', 'config', '--global', 'user.name', name], check=True)
            subprocess.run(['git', 'config', '--global', 'user.email', email], check=True)
            return GitHubManager.get_git_config()
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def get_full_status():
        """Get combined GitHub status"""
        return {
            'ssh': GitHubManager.get_ssh_status(),
            'gh_cli': GitHubManager.get_gh_cli_status(),
            'git_config': GitHubManager.get_git_config()
        }


class ClaudeTaskManager:
    """Manages Claude Code tasks running in tmux sessions"""

    TASKS_DIR = '/home/dev/.claude-tasks'
    TOKEN_FILE = '/home/dev/.claude-tasks/.api-token'

    @staticmethod
    def ensure_tasks_dir():
        os.makedirs(ClaudeTaskManager.TASKS_DIR, mode=0o700, exist_ok=True)

    @staticmethod
    def get_or_create_token():
        ClaudeTaskManager.ensure_tasks_dir()
        if os.path.exists(ClaudeTaskManager.TOKEN_FILE):
            with open(ClaudeTaskManager.TOKEN_FILE, 'r') as f:
                token = f.read().strip()
                if token:
                    return token
        token = secrets.token_urlsafe(36)
        with open(ClaudeTaskManager.TOKEN_FILE, 'w') as f:
            f.write(token)
        os.chmod(ClaudeTaskManager.TOKEN_FILE, 0o600)
        return token

    @staticmethod
    def verify_token(token):
        if not os.path.exists(ClaudeTaskManager.TOKEN_FILE):
            return False
        with open(ClaudeTaskManager.TOKEN_FILE, 'r') as f:
            stored = f.read().strip()
        return secrets.compare_digest(token, stored)

    @staticmethod
    def regenerate_token():
        ClaudeTaskManager.ensure_tasks_dir()
        token = secrets.token_urlsafe(36)
        with open(ClaudeTaskManager.TOKEN_FILE, 'w') as f:
            f.write(token)
        os.chmod(ClaudeTaskManager.TOKEN_FILE, 0o600)
        return token

    # ── Assistant selection ──────────────────────────────────────────────
    # Assistant options surfaced in the dashboard dropdown:
    #   1. Claude Code   — always available (anthropic-hosted)
    #   2. Ante CLI      — always available (pre-installed in the image)
    #   3. LibreFang     — agent-OS CLI; listed when its binary is present
    #   4. OpenRouter    — OpenCode CLI proxied through OpenRouter
    #   5. DeepSeek      — OpenCode CLI against DeepSeek's native API
    #   6. Opensource GPU — kc-harness against the configured Ollama endpoint
    # The legacy `opencode-fallback` assistant was retired in favour of
    # kc-harness: same endpoint, narrow tool surface, XML-aware parser, so
    # small local models actually execute tools instead of describing them.
    ASSISTANTS = {
        'claude': {
            'id': 'claude',
            'label': 'Claude Code',
        },
        'ante': {
            'id': 'ante',
            'label': 'Ante CLI',
        },
        # LibreFang — open-source agent OS (https://librefang.ai). Tasks talk
        # to its registry-bundled "coder" agent via `librefang chat`; the CLI
        # picks up whatever provider key is in the environment
        # (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, …).
        'librefang': {
            'id': 'librefang',
            'label': 'LibreFang',
        },
        'opencode-openrouter': {
            'id': 'opencode-openrouter',
            'label': 'OpenRouter',
        },
        'opencode-deepseek': {
            'id': 'opencode-deepseek',
            'label': 'DeepSeek',
        },
        # kc-harness — thin in-pod LLM tool-call loop at /tmp/browser/harness.py
        # See charts/workspace/harness.py for the design rationale.
        'kc-harness': {
            'id': 'kc-harness',
            'label': 'Opensource GPU',
        },
    }

    @staticmethod
    def available_assistants():
        out = [dict(ClaudeTaskManager.ASSISTANTS['claude'], default=True)]
        out.append(dict(ClaudeTaskManager.ASSISTANTS['ante']))
        # LibreFang — listed only when its CLI is actually resolvable (older
        # images predate it, and /usr/local/bin/librefang is a symlink to a
        # PVC path that start.sh seeds), so the dropdown never advertises a
        # dead option.
        if shutil.which('librefang'):
            out.append(dict(ClaudeTaskManager.ASSISTANTS['librefang']))
        if os.environ.get('OPENROUTER_API_KEY'):
            out.append(dict(
                ClaudeTaskManager.ASSISTANTS['opencode-openrouter'],
                model=os.environ.get('KC_OPENROUTER_MODEL', 'anthropic/claude-sonnet-4'),
            ))
        if os.environ.get('DEEPSEEK_API_KEY'):
            out.append(dict(
                ClaudeTaskManager.ASSISTANTS['opencode-deepseek'],
                model=os.environ.get('KC_DEEPSEEK_MODEL', 'deepseek-chat'),
            ))
        if os.environ.get('KC_FALLBACK_BASE_URL'):
            out.append(dict(
                ClaudeTaskManager.ASSISTANTS['kc-harness'],
                model=os.environ.get('KC_HARNESS_MODEL')
                      or os.environ.get('KC_FALLBACK_MODEL', 'qwen3:32b-q4_K_M'),
            ))
        return out

    @staticmethod
    def resolve_assistant(requested):
        """Validate the caller's choice; fall back to claude on anything
        unknown or disabled (the dashboard hides disabled options, but
        webhooks/crons/CLI clients are free-form so we defend the boundary)."""
        enabled = {a['id'] for a in ClaudeTaskManager.available_assistants()}
        if requested and requested in enabled:
            return requested
        return 'claude'

    @staticmethod
    def assistant_command(assistant):
        if assistant == 'ante':
            return 'ante'
        if assistant == 'librefang':
            # Interactive chat REPL with the registry's "coder" agent (synced
            # into ~/.librefang by `librefang init`). KC_LIBREFANG_AGENT
            # overrides the agent name for users who ship their own manifest.
            # Quoted so a hostile env var can't break out of the `bash -lc`
            # shell_cmd built downstream in create_task().
            #
            # `librefang chat` needs the kernel daemon running — without it the
            # CLI panics ("there is no reactor running") and the tmux session
            # exits instantly. `librefang start` self-daemonizes and is a no-op
            # when already up; poll status briefly so the REPL doesn't attach
            # before the daemon's API binds. Mirrors the headless bootstrap in
            # mcp_agent_orchestrator.py.
            agent = _shell_quote(os.environ.get('KC_LIBREFANG_AGENT', 'coder'))
            return (
                'librefang status -q >/dev/null 2>&1 || { '
                'librefang start >/dev/null 2>&1 || true; '
                'for _ in 1 2 3 4 5 6 7 8 9 10; do '
                'librefang status -q >/dev/null 2>&1 && break; sleep 1; '
                'done; }; '
                f'librefang chat {agent}'
            )
        if assistant == 'opencode-openrouter':
            model = os.environ.get('KC_OPENROUTER_MODEL', 'anthropic/claude-sonnet-4')
            # Quote the model so a hostile env var can't break out of the
            # `bash -lc` shell_cmd built downstream in create_task().
            return f'opencode --model {_shell_quote(f"openrouter/{model}")}'
        if assistant == 'opencode-deepseek':
            model = os.environ.get('KC_DEEPSEEK_MODEL', 'deepseek-chat')
            return f'opencode --model {_shell_quote(f"deepseek/{model}")}'
        if assistant == 'kc-harness':
            # Reads stdin (tmux paste) and emits dashboard JSONL events.
            # KC_HARNESS_MODEL / KC_FALLBACK_MODEL pick the model; the
            # default lives in harness.py.
            return 'python3 /tmp/browser/harness.py'
        return 'claude'

    @staticmethod
    def create_task(prompt, workdir=None, response_url=None, response_secret=None,
                    source=None, disable_memory_injection=False, assistant=None,
                    parent_task_id=None):
        ClaudeTaskManager.ensure_tasks_dir()
        assistant = ClaudeTaskManager.resolve_assistant(assistant)
        task_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        session_id = str(uuid.uuid4())
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        os.makedirs(task_dir, mode=0o700)

        if workdir is None:
            workdir = '/home/dev'

        session_name = f'claude-{task_id}'

        # ── Memory auto-injection (opt-in, OFF by default) ────────────────
        # Optionally compute a <workspace_memories> block from top-K relevant
        # memories and prepend it to the pasted prompt. This is now OFF by
        # default: it front-loaded a large block of memories into every new
        # session (especially noisy for Ante), and the agent can pull
        # memories on demand via the memory MCP tools — which CLAUDE.md
        # already documents. Set KC_MEMORY_PREINJECT=1 to restore the old
        # prepend behavior. `disable_memory_injection` still force-disables.
        _preinject = os.environ.get('KC_MEMORY_PREINJECT', '').strip().lower() \
            in ('1', 'true', 'yes', 'on')
        injected_memories = []
        injection_block = ''
        if _MEMORY_AVAILABLE and _preinject and not disable_memory_injection:
            try:
                injected_memories = MemoryManager.top_for_prompt(prompt or '')
                injection_block = MemoryManager.format_injection_block(injected_memories)
            except Exception as e:  # never fail task creation on memory errors
                print(f'[memory] auto-inject failed: {e}', file=sys.stderr)
                injected_memories = []
                injection_block = ''

        meta = {
            'task_id': task_id,
            'session_id': session_id,
            'prompt': prompt,
            'workdir': workdir,
            'status': 'running',
            'created_at': time.time(),
            'tmux_session': session_name,
            'assistant': assistant,
            'parent_task_id': parent_task_id,
            'sub_task_ids': [],
            'memory_injected': [
                {'namespace': m.get('namespace'), 'key': m.get('key')}
                for m in injected_memories
            ],
        }
        if disable_memory_injection:
            meta['memory_injection_disabled'] = True
        # Optional completion-hook fields. When response_url is set, the server
        # POSTs the final task state (status + tail output) to that URL once the
        # task reaches a terminal state. response_secret, if present, is used to
        # HMAC-SHA256-sign the body (X-Kube-Coder-Signature-256: sha256=...).
        # `source` is a free-form string ('webhook:<id>', 'cron:<id>', etc.)
        # used by the dashboard to badge triggered tasks.
        if response_url:
            meta['response_url'] = response_url
        if response_secret:
            meta['response_secret'] = response_secret
        if source:
            meta['source'] = source

        meta_path = os.path.join(task_dir, 'task.json')
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

        # Write prompt to a file so we can paste it cleanly via tmux. We
        # prepend the memory-injection block here so the model sees prior
        # context before the user's actual request.
        prompt_file = os.path.join(task_dir, 'prompt.txt')
        with open(prompt_file, 'w') as f:
            f.write(injection_block)
            f.write(prompt)

        # Log read-access for every auto-injected memory (best-effort).
        if injected_memories and _MEMORY_AVAILABLE:
            for m in injected_memories:
                try:
                    MemoryManager.log_ref(
                        namespace=m['namespace'], key=m['key'],
                        ref_kind='task', ref_id=task_id, access_kind='read',
                    )
                except Exception:
                    pass

        # Launch the interactive assistant CLI in a tmux session. We export
        # KC_TASK_ID into the session env so the MCP memory server (spawned
        # by the assistant) can attribute writes to this task. The CLI is
        # chosen per task: Claude Code by default; OpenCode (via OpenRouter
        # or a custom fallback endpoint) when those providers are configured
        # on the workspace and the caller passes the matching `assistant`
        # value. See ClaudeTaskManager.assistant_command().
        cli_cmd = ClaudeTaskManager.assistant_command(assistant)
        shell_cmd = f'cd {_shell_quote(workdir)} && {cli_cmd}'
        tmux_cmd = [
            'tmux', 'new-session', '-d',
            '-s', session_name,
            '-x', '220', '-y', '50',
            '-e', f'KC_TASK_ID={task_id}',
            'bash', '-lc', shell_cmd,
        ]

        result = subprocess.run(tmux_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            meta['status'] = 'error'
            meta['error'] = result.stderr.strip()
            with open(meta_path, 'w') as f:
                json.dump(meta, f, indent=2)
            return meta

        # Mirror the tmux pane output to a log file so it survives session/pod restarts.
        # `pipe-pane -o` toggles output piping; the appended `cat >> ...` keeps writing
        # for the lifetime of the session.
        output_log = os.path.join(task_dir, 'output.log')
        subprocess.run(
            ['tmux', 'pipe-pane', '-o', '-t', session_name,
             f'cat >> {_shell_quote(output_log)}'],
            capture_output=True, text=True,
        )

        # Send the initial prompt to the interactive claude session after it starts
        # Use tmux load-buffer + paste-buffer for clean multi-line handling
        def send_prompt():
            time.sleep(3)  # Wait for claude to initialize
            try:
                subprocess.run(
                    ['tmux', 'load-buffer', '-b', f'prompt-{task_id}', prompt_file],
                    capture_output=True, text=True, check=True,
                )
                subprocess.run(
                    ['tmux', 'paste-buffer', '-b', f'prompt-{task_id}', '-t', session_name],
                    capture_output=True, text=True, check=True,
                )
                subprocess.run(
                    ['tmux', 'send-keys', '-t', session_name, 'Enter'],
                    capture_output=True, text=True,
                )
                subprocess.run(
                    ['tmux', 'delete-buffer', '-b', f'prompt-{task_id}'],
                    capture_output=True, text=True,
                )
            except Exception as e:
                print(f"[ClaudeTaskManager] Failed to send prompt: {e}")

        threading.Thread(target=send_prompt, daemon=True).start()

        return meta

    @staticmethod
    def create_terminal_task(workdir=None):
        """Create a task that runs an interactive bash session under tmux.

        Mirrors create_task() but skips launching claude and pasting a prompt —
        useful so the dashboard's Terminal button leaves a row in the task
        list that can be re-attached later, even if the original browser tab
        is closed.
        """
        ClaudeTaskManager.ensure_tasks_dir()
        task_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        session_id = str(uuid.uuid4())
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        os.makedirs(task_dir, mode=0o700)

        if workdir is None:
            workdir = '/home/dev'

        session_name = f'claude-{task_id}'

        meta = {
            'task_id': task_id,
            'session_id': session_id,
            'kind': 'terminal',
            'prompt': f'Terminal · {workdir}',
            'workdir': workdir,
            'status': 'running',
            'created_at': time.time(),
            'tmux_session': session_name,
        }

        meta_path = os.path.join(task_dir, 'task.json')
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

        shell_cmd = f'cd {_shell_quote(workdir)} && exec bash -l'
        tmux_cmd = [
            'tmux', 'new-session', '-d',
            '-s', session_name,
            '-x', '220', '-y', '50',
            'bash', '-lc', shell_cmd,
        ]
        result = subprocess.run(tmux_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            meta['status'] = 'error'
            meta['error'] = result.stderr.strip()
            with open(meta_path, 'w') as f:
                json.dump(meta, f, indent=2)
            return meta

        output_log = os.path.join(task_dir, 'output.log')
        subprocess.run(
            ['tmux', 'pipe-pane', '-o', '-t', session_name,
             f'cat >> {_shell_quote(output_log)}'],
            capture_output=True, text=True,
        )

        return meta

    @staticmethod
    def list_tasks(parent=None):
        ClaudeTaskManager.ensure_tasks_dir()
        tasks = []
        try:
            entries = sorted(os.listdir(ClaudeTaskManager.TASKS_DIR), reverse=True)
        except OSError:
            return tasks

        for entry in entries:
            task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, entry)
            meta_path = os.path.join(task_dir, 'task.json')
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                ClaudeTaskManager._reconcile_status(meta, task_dir)

                # Filter by parent_task_id when requested
                task_parent = meta.get('parent_task_id')
                if parent is not None and task_parent != parent:
                    continue

                tasks.append({
                    'task_id': meta.get('task_id', entry),
                    'name': meta.get('name'),
                    'prompt': meta.get('prompt', '')[:120],
                    'status': meta.get('status', 'unknown'),
                    'created_at': meta.get('created_at'),
                    'finished_at': meta.get('finished_at') or meta.get('killed_at'),
                    # Moment the rendered screen last changed — drives the
                    # dashboard's idle-duration label + stale escalation.
                    'last_activity_at': meta.get('last_activity_at'),
                    'source': meta.get('source'),
                    'kind': meta.get('kind', 'claude'),
                    'assistant': meta.get('assistant'),
                    'parent_task_id': task_parent,
                    'sub_task_ids': meta.get('sub_task_ids', []),
                    'memory_injected': meta.get('memory_injected', []),
                    'memory_injection_disabled':
                        bool(meta.get('memory_injection_disabled')),
                })
            except (json.JSONDecodeError, OSError):
                continue
        return tasks

    @staticmethod
    def get_task(task_id):
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None
        with open(meta_path, 'r') as f:
            meta = json.load(f)
        ClaudeTaskManager._reconcile_status(meta, task_dir)

        # Get recent output from live tmux pane or fallback to log file
        recent_output = ''
        session_name = meta.get('tmux_session', f'claude-{task_id}')
        result = subprocess.run(
            ['tmux', 'capture-pane', '-J', '-t', session_name, '-p', '-S', '-50'],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            recent_output = result.stdout
        meta['recent_output'] = recent_output
        return meta

    @staticmethod
    def get_task_output(task_id, tail=None):
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None

        with open(meta_path, 'r') as f:
            meta = json.load(f)

        # For live sessions, capture the tmux pane content
        session_name = meta.get('tmux_session', f'claude-{task_id}')
        result = subprocess.run(
            # -J joins wrapped lines, so URLs the assistant prints that
            # overflow the 220-col pane come back as one logical line —
            # critical for the SPA's URL-detection strip in the Terminal tab.
            ['tmux', 'capture-pane', '-J', '-t', session_name, '-p', '-S', '-200'],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            output = result.stdout
            if tail:
                lines = output.split('\n')
                return '\n'.join(lines[-tail:])
            return output

        # Fallback to output.log if session is gone
        output_path = os.path.join(task_dir, 'output.log')
        if os.path.exists(output_path):
            with open(output_path, 'r', errors='replace') as f:
                if tail:
                    lines = f.readlines()
                    return ''.join(lines[-tail:])
                return f.read()
        return '(no output available)'

    @staticmethod
    def send_followup(task_id, prompt):
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None, 'Task not found'

        with open(meta_path, 'r') as f:
            meta = json.load(f)

        session_name = meta.get('tmux_session', f'claude-{task_id}')

        # Check if tmux session is still alive
        check = subprocess.run(
            ['tmux', 'has-session', '-t', session_name],
            capture_output=True, text=True,
        )
        if check.returncode != 0:
            return None, 'Session is no longer running'

        # Send the follow-up prompt into the interactive claude session
        # Use load-buffer + paste-buffer for clean multi-line handling
        prompt_file = os.path.join(task_dir, 'followup.txt')
        with open(prompt_file, 'w') as f:
            f.write(prompt)

        try:
            buf_name = f'followup-{task_id}'
            subprocess.run(
                ['tmux', 'load-buffer', '-b', buf_name, prompt_file],
                capture_output=True, text=True, check=True,
            )
            subprocess.run(
                ['tmux', 'paste-buffer', '-b', buf_name, '-t', session_name],
                capture_output=True, text=True, check=True,
            )
            subprocess.run(
                ['tmux', 'send-keys', '-t', session_name, 'Enter'],
                capture_output=True, text=True,
            )
            subprocess.run(
                ['tmux', 'delete-buffer', '-b', buf_name],
                capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            return None, f'Failed to send follow-up: {e}'

        # Update metadata under an exclusive lock so concurrent /message calls
        # don't drop each other's appends to followups[].
        sent_at = time.time()

        def mutate(m):
            m['status'] = 'running'
            fps = m.get('followups', [])
            fps.append({'prompt': prompt, 'sent_at': sent_at})
            m['followups'] = fps

        updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
        return updated, None

    @staticmethod
    def delete_task(task_id):
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None

        with open(meta_path, 'r') as f:
            meta = json.load(f)

        session_name = meta.get('tmux_session', f'claude-{task_id}')

        # Kill the tmux session if alive
        subprocess.run(
            ['tmux', 'kill-session', '-t', session_name],
            capture_output=True, text=True,
        )

        killed_at = time.time()
        fire_hook = False

        def mutate(m):
            nonlocal fire_hook
            m['status'] = 'killed'
            m['killed_at'] = killed_at
            if m.get('response_url') and not m.get('hook_fired_at'):
                m['hook_fired_at'] = killed_at
                fire_hook = True

        updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
        if updated is not None and fire_hook:
            ClaudeTaskManager._fire_completion_hook(updated)
        return updated

    @staticmethod
    def rename_task(task_id, body):
        """Rename a task. Returns (meta, error).

        Empty/whitespace-only name clears the field. Cap 100 chars after
        stripping control characters.
        """
        if 'name' not in body:
            return None, 'name field required'
        raw = body['name']
        if not isinstance(raw, str):
            return None, 'name must be a string'
        cleaned = ''.join(
            ch for ch in raw if ch == ' ' or ch.isprintable()
        ).strip()
        if len(cleaned) > 100:
            return None, 'name too long (max 100)'

        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        if not os.path.isdir(task_dir):
            return None, 'not_found'

        renamed_at = time.time()

        def mutate(m):
            if cleaned:
                m['name'] = cleaned
                m['renamed_at'] = renamed_at
            else:
                m.pop('name', None)
                m.pop('renamed_at', None)

        updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
        if updated is None:
            return None, 'not_found'
        return updated, None

    @staticmethod
    def _atomic_update_meta(task_dir, mutate_fn):
        """Atomically read-modify-write task.json under an exclusive flock.

        mutate_fn(meta) may modify meta in place. Returning False skips the write
        (used when the mutator decides the update is no longer needed after seeing
        fresh state). Returns the post-mutation meta dict, or None if the task
        directory is gone.
        """
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None
        lock_path = os.path.join(task_dir, '.meta.lock')
        with open(lock_path, 'a') as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                should_write = mutate_fn(meta)
                if should_write is False:
                    return meta
                tmp_path = meta_path + '.tmp'
                with open(tmp_path, 'w') as f:
                    json.dump(meta, f, indent=2)
                os.rename(tmp_path, meta_path)
                return meta
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)

    @staticmethod
    def _is_safe_response_url(url):
        """Allow only http(s) URLs to public IPs. Reject:
          - non-http(s) schemes (file://, gopher://) — they turn urlopen() into
            an SSRF / local-file primitive
          - hosts that resolve to RFC1918, link-local, loopback or unspecified
            ranges — would let an attacker probe the cloud metadata service
            (169.254.169.254), in-cluster services (10.x), or the workspace
            itself (localhost)
        Set ALLOW_INTERNAL_HOOKS=true to opt back in (single-user / trusted
        deploys that need to POST hook results into the cluster)."""
        if not url or not isinstance(url, str):
            return False
        try:
            parsed = urllib.parse.urlparse(url)
        except (ValueError, TypeError):
            return False
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return False
        if ALLOW_INTERNAL_HOOKS:
            return True
        host = parsed.hostname or ''
        if not host:
            return False
        # Resolve to *all* addresses; reject if any one is internal — DNS
        # rebinding can return an internal IP on the second lookup, so the
        # set-must-be-all-public check has to apply here. Unresolvable
        # hostnames pass through — urlopen will fail safely at fire time
        # and there's no SSRF target to actually hit.
        import socket
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, UnicodeError):
            return True
        import ipaddress
        for info in infos:
            sockaddr = info[4]
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except (ValueError, IndexError):
                return False
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_unspecified
                    or ip.is_reserved):
                return False
        return True

    @staticmethod
    def _fire_completion_hook(meta):
        """POST the task's terminal state to meta['response_url'] in a daemon thread.

        Idempotent: callers set meta['hook_fired_at'] under the meta lock before
        invoking this, so duplicate transitions (e.g. concurrent reconciles) don't
        re-send. If response_secret is set, the body is signed with HMAC-SHA256
        and the digest goes in X-Kube-Coder-Signature-256: sha256=<hex>.

        Network failures are logged and swallowed — the task itself is unaffected.
        Retries are intentionally not implemented here; if callers need at-least-once
        delivery they should layer their own queue on top.
        """
        url = meta.get('response_url')
        if not ClaudeTaskManager._is_safe_response_url(url):
            if url:
                print(f'[completion-hook] task={meta.get("task_id")} skipped: unsafe URL scheme', file=sys.stderr)
            return
        try:
            tail_output = ClaudeTaskManager.get_task_output(meta.get('task_id', ''), tail=200) or ''
        except Exception:
            tail_output = ''
        payload = {
            'task_id': meta.get('task_id'),
            'status': meta.get('status'),
            'prompt': meta.get('prompt'),
            'workdir': meta.get('workdir'),
            'source': meta.get('source'),
            'created_at': meta.get('created_at'),
            'finished_at': meta.get('finished_at') or meta.get('killed_at'),
            'output': tail_output,
        }
        body = json.dumps(payload).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'kube-coder-completion-hook/1.0',
        }
        secret = meta.get('response_secret')
        if secret:
            sig = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
            headers['X-Kube-Coder-Signature-256'] = f'sha256={sig}'

        task_id = meta.get('task_id', '?')

        def _send():
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=10) as resp:
                    print(f'[completion-hook] task={task_id} -> {url} ({resp.status})')
            except Exception as e:
                print(f'[completion-hook] task={task_id} -> {url} FAILED: {e}', file=sys.stderr)

        threading.Thread(target=_send, daemon=True).start()

    @staticmethod
    def _reconcile_status(meta, task_dir):
        """If task.json says running but tmux session is gone, update status.
        Also check for waiting-for-input patterns in running tasks."""
        current_status = meta.get('status', 'unknown')
        
        # If task is already finished, no need to check further
        if current_status not in ('running', 'waiting-for-input'):
            return

        session_name = meta.get('tmux_session', '')
        if not session_name:
            return

        check = subprocess.run(
            ['tmux', 'has-session', '-t', session_name],
            capture_output=True, text=True,
        )
        
        # If tmux session is gone, mark as completed
        if check.returncode != 0:
            finished_at = time.time()
            fire_hook = False

            def mutate(m):
                nonlocal fire_hook
                # Re-check inside the lock; another reconcile may have run already.
                if m.get('status') not in ('running', 'waiting-for-input'):
                    return False
                m['status'] = 'completed'
                m['finished_at'] = finished_at
                # Clear waiting state fields
                m.pop('waiting_for_input', None)
                m.pop('last_input_prompt', None)
                # Decide-and-mark-fired atomically. If we mark hook_fired_at here, a
                # concurrent reconciler reading the same task.json will see it and
                # skip firing — so we get at-most-once delivery without needing a
                # second lock acquire.
                if m.get('response_url') and not m.get('hook_fired_at'):
                    m['hook_fired_at'] = finished_at
                    fire_hook = True

            updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
            if updated is not None:
                meta['status'] = updated.get('status', meta.get('status'))
                meta['finished_at'] = updated.get('finished_at', meta.get('finished_at'))
                meta.pop('waiting_for_input', None)
                meta.pop('last_input_prompt', None)
                if fire_hook:
                    ClaudeTaskManager._fire_completion_hook(updated)
            return
        
        # Session is alive — derive waiting-for-input from render *quiescence*
        # rather than scraping prompt text (which never worked across the
        # full-screen TUIs Claude/Ante/OpenCode render). While an agent works
        # it streams output / animates a spinner+timer, so the captured screen
        # keeps changing; once it finishes a turn or hits a prompt the screen
        # goes static. Stable for >= IDLE_WAITING_SECONDS ⇒ waiting-for-input.
        # `last_activity_at` (the moment the screen last changed) also lets the
        # dashboard show idle duration and escalate long-idle ("stale") tasks.
        capture_cmd = subprocess.run(
            ['tmux', 'capture-pane', '-t', session_name, '-p'],
            capture_output=True, text=True,
        )
        if capture_cmd.returncode != 0:
            return
        screen = strip_ansi(capture_cmd.stdout or '')
        digest = hashlib.sha1(screen.encode('utf-8', 'replace')).hexdigest()
        now = time.time()

        if digest != meta.get('pane_hash'):
            # Screen changed → activity. Record it, reset the idle clock, and
            # if we had flagged waiting, return to running.
            def mutate(m):
                m['pane_hash'] = digest
                m['last_activity_at'] = now
                if m.get('status') == 'waiting-for-input':
                    m['status'] = 'running'
                    m.pop('waiting_for_input', None)
                    m.pop('last_input_prompt', None)

            updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
            if updated is not None:
                meta['pane_hash'] = digest
                meta['last_activity_at'] = now
                if meta.get('status') == 'waiting-for-input':
                    meta['status'] = 'running'
                    meta.pop('waiting_for_input', None)
                    meta.pop('last_input_prompt', None)
            return

        # Screen unchanged since the previous capture.
        if current_status == 'running':
            stable_since = meta.get('last_activity_at') or now
            if now - stable_since >= IDLE_WAITING_SECONDS:
                def mutate(m):
                    if m.get('status') == 'running':  # re-check inside lock
                        m['status'] = 'waiting-for-input'
                        m['waiting_for_input'] = True

                updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
                if updated is not None:
                    meta['status'] = 'waiting-for-input'
                    meta['waiting_for_input'] = True


def _shell_quote(s):
    """Quote a string for safe use in a shell command."""
    import shlex
    return shlex.quote(s)


class WorkspaceManager:
    """Lists candidate working directories under /home/dev for the
    new-task picker. Skips hidden tooling dirs (.config, .credentials,
    .claude-tasks, etc.) and obvious non-projects (node_modules, vendor)."""

    HOME_DIR = '/home/dev'
    PROJECT_MARKERS = (
        'package.json', 'pyproject.toml', 'Cargo.toml',
        'go.mod', 'Gemfile', 'Makefile', 'requirements.txt',
    )
    SKIP_NAMES = {'node_modules', 'vendor', 'target', 'dist', 'build', '__pycache__'}

    @staticmethod
    def list_dirs():
        results = []
        try:
            entries = os.listdir(WorkspaceManager.HOME_DIR)
        except OSError:
            return results
        for name in entries:
            if name.startswith('.'):
                continue
            if name in WorkspaceManager.SKIP_NAMES:
                continue
            path = os.path.join(WorkspaceManager.HOME_DIR, name)
            if not os.path.isdir(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0
            is_git = os.path.isdir(os.path.join(path, '.git'))
            has_project_marker = any(
                os.path.exists(os.path.join(path, m))
                for m in WorkspaceManager.PROJECT_MARKERS
            )
            results.append({
                'path': path,
                'label': name,
                'is_git_repo': is_git,
                'is_project': is_git or has_project_marker,
                'mtime': mtime,
            })
        results.sort(key=lambda d: d['mtime'], reverse=True)
        return results


class _ReplayCache:
    """Bounded LRU+TTL set of (webhook_id, body_sha256) keys for replay
    protection. In-memory only — fine for a single-pod workspace; if we ever
    horizontal-scale the IDE pod, this moves to Redis.

    The size cap (default 1024) protects against memory growth under a flood
    of distinct payloads; the TTL (default 5 min) is the replay window. A key
    is rejected if seen before its TTL expires."""

    def __init__(self, capacity=1024, ttl_seconds=300, clock=time.time):
        self._cap = capacity
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # OrderedDict so we can evict oldest via popitem(last=False).
        self._seen = collections.OrderedDict()

    def check_and_record(self, key):
        """Return True if this key is fresh (record it); False if it's a replay
        within the TTL window."""
        now = self._clock()
        with self._lock:
            # Lazy TTL eviction at the head; OrderedDict is insertion-ordered.
            while self._seen:
                k, ts = next(iter(self._seen.items()))
                if now - ts > self._ttl:
                    self._seen.popitem(last=False)
                else:
                    break
            if key in self._seen:
                # Refresh position to LRU-end so an actively-replayed key stays hot
                # (and continues to be rejected) instead of aging out.
                self._seen.move_to_end(key)
                self._seen[key] = now
                return False
            self._seen[key] = now
            # Size cap — drop oldest after insert
            while len(self._seen) > self._cap:
                self._seen.popitem(last=False)
            return True


class WebhookManager:
    """Inbound HTTP webhooks that spawn Claude tasks.

    A webhook config is a JSON file at /home/dev/.claude-triggers/webhooks/<id>.json:

        {
          "id":               "github-pr-review",
          "prompt_template":  "Review the PR titled '{{ payload.pull_request.title }}'",
          "workdir":          "/home/dev/myproject",
          "interpolate_mode": "attach",     // "attach" (default, safe) or "interpolate"
          "hmac_secret":      "<random>",   // optional but recommended
          "signature_header": "X-Hub-Signature-256",  // header name to verify
          "signature_algo":   "sha256",     // sha256 (default) or sha1
          "response_url":     "https://...", // optional — POST task result back here
          "response_secret":  "...",         // optional HMAC for the response POST
          "created_at":       <epoch>
        }

    The receiver endpoint POST /api/webhooks/<id> is unauthenticated by bearer
    token on purpose — it's meant to be called by external services (GitHub,
    Stripe, Slack, etc.). Auth is via HMAC of the raw body against hmac_secret.
    If hmac_secret is omitted, the webhook is open — only do that for testing.
    """

    WEBHOOKS_DIR = '/home/dev/.claude-triggers/webhooks'
    _ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
    _INTERP_RE = re.compile(r'\{\{\s*payload((?:\.[\w]+)*)\s*\}\}')
    PROVIDERS = ('generic', 'github', 'slack', 'stripe')
    # Module-level so the cache survives across requests (each request gets a
    # fresh handler instance). 5-minute window matches Slack/Stripe convention.
    REPLAY_CACHE = _ReplayCache(capacity=1024, ttl_seconds=300)
    # Tolerated clock skew for providers that sign a timestamp (Slack, Stripe).
    # Matches Slack's documented 5-minute drift allowance.
    TIMESTAMP_TOLERANCE = 300

    @staticmethod
    def ensure_dir():
        os.makedirs(WebhookManager.WEBHOOKS_DIR, mode=0o700, exist_ok=True)

    @staticmethod
    def _config_path(webhook_id):
        return os.path.join(WebhookManager.WEBHOOKS_DIR, f'{webhook_id}.json')

    @staticmethod
    def valid_id(webhook_id):
        return bool(webhook_id) and bool(WebhookManager._ID_RE.match(webhook_id))

    @staticmethod
    def list_webhooks():
        WebhookManager.ensure_dir()
        out = []
        try:
            entries = sorted(os.listdir(WebhookManager.WEBHOOKS_DIR))
        except OSError:
            return out
        for name in entries:
            if not name.endswith('.json'):
                continue
            path = os.path.join(WebhookManager.WEBHOOKS_DIR, name)
            try:
                with open(path) as f:
                    cfg = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            out.append(WebhookManager._public_view(cfg))
        return out

    @staticmethod
    def get_webhook(webhook_id, include_secrets=False):
        if not WebhookManager.valid_id(webhook_id):
            return None
        try:
            with open(WebhookManager._config_path(webhook_id)) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return cfg if include_secrets else WebhookManager._public_view(cfg)

    @staticmethod
    def _public_view(cfg):
        """Return a copy of the config safe to expose over the dashboard API:
        secret material is replaced with a boolean indicator so the UI can
        show 'configured' without revealing the value."""
        view = dict(cfg)
        for k in ('hmac_secret', 'response_secret'):
            if view.get(k):
                view[k + '_set'] = True
                view.pop(k)
        return view

    @staticmethod
    def create_or_update(data, existing_id=None):
        """Validate and persist a webhook config. Returns (cfg, error_str)."""
        WebhookManager.ensure_dir()
        webhook_id = existing_id or data.get('id', '')
        if not WebhookManager.valid_id(webhook_id):
            return None, 'invalid id (1-64 chars, [a-zA-Z0-9_-])'
        prompt_template = (data.get('prompt_template') or '').strip()
        if not prompt_template:
            return None, 'prompt_template is required'

        mode = data.get('interpolate_mode', 'attach')
        if mode not in ('attach', 'interpolate'):
            return None, "interpolate_mode must be 'attach' or 'interpolate'"

        algo = data.get('signature_algo', 'sha256')
        if algo not in ('sha256', 'sha1'):
            return None, "signature_algo must be 'sha256' or 'sha1'"

        provider = data.get('provider', 'generic')
        if provider not in WebhookManager.PROVIDERS:
            return None, f'provider must be one of {WebhookManager.PROVIDERS}'

        response_url = data.get('response_url')
        if response_url and not ClaudeTaskManager._is_safe_response_url(response_url):
            return None, 'response_url must be http(s)'

        # Default signature_header by provider. Users can override, but the
        # defaults match what each platform documents so most setups are
        # zero-config.
        default_header = {
            'github': 'X-Hub-Signature-256',
            'slack': 'X-Slack-Signature',
            'stripe': 'Stripe-Signature',
            'generic': 'X-Hub-Signature-256',
        }[provider]
        cfg = {
            'id': webhook_id,
            'prompt_template': prompt_template,
            'workdir': data.get('workdir') or '/home/dev',
            'interpolate_mode': mode,
            'provider': provider,
            'signature_header': data.get('signature_header') or default_header,
            'signature_algo': algo,
            'created_at': time.time(),
        }
        # Optional secret-bearing fields. Auto-mint hmac_secret on create if
        # caller didn't provide one — never want to silently land an open webhook.
        if data.get('hmac_secret'):
            cfg['hmac_secret'] = data['hmac_secret']
        elif not existing_id:
            cfg['hmac_secret'] = secrets.token_urlsafe(32)
        if data.get('response_url'):
            cfg['response_url'] = data['response_url']
        if data.get('response_secret'):
            cfg['response_secret'] = data['response_secret']

        # Preserve created_at on update
        if existing_id:
            prior = WebhookManager.get_webhook(existing_id, include_secrets=True) or {}
            if prior.get('created_at'):
                cfg['created_at'] = prior['created_at']
            # If caller didn't pass hmac_secret on update, keep the prior one.
            if 'hmac_secret' not in cfg and prior.get('hmac_secret'):
                cfg['hmac_secret'] = prior['hmac_secret']

        path = WebhookManager._config_path(webhook_id)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)
        return cfg, None

    @staticmethod
    def delete(webhook_id):
        if not WebhookManager.valid_id(webhook_id):
            return False
        path = WebhookManager._config_path(webhook_id)
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def verify_signature(cfg, raw_body, headers):
        """Provider-aware signature verification.

        ``headers`` accepts either:
          * a dict-like with case-insensitive ``.get(name, default)`` — typically
            ``BaseHTTPRequestHandler.headers``. Required for Slack/Stripe which
            read multiple headers.
          * a plain string, treated as the value of ``cfg['signature_header']``.
            Kept as a backwards-compat path for the original generic-HMAC tests
            and for callers that already extracted the one header they need.

        If the webhook has no ``hmac_secret`` configured, returns True (open
        mode — only intended for dev/testing; create() auto-mints one to
        discourage this in production).
        """
        secret = cfg.get('hmac_secret')
        if not secret:
            return True

        provider = cfg.get('provider', 'generic')

        # Normalize headers into a uniform `get(name, default)`. For the str
        # form (or None for "no header sent"), only the configured
        # signature_header resolves; everything else returns ''.
        if headers is None or isinstance(headers, str):
            target = (cfg.get('signature_header') or '').lower()
            value = headers or ''

            def _get(name, default=''):
                return value if name.lower() == target else default
        else:
            def _get(name, default=''):
                v = headers.get(name, default)
                return v if v is not None else default

        if provider == 'slack':
            return WebhookManager._verify_slack(secret, raw_body, _get)
        if provider == 'stripe':
            return WebhookManager._verify_stripe(secret, raw_body, _get)
        # 'generic' and 'github' use the same shape: hex HMAC, optional
        # algo-prefix, configured header name.
        return WebhookManager._verify_generic(cfg, secret, raw_body, _get)

    @staticmethod
    def _verify_generic(cfg, secret, raw_body, get_header):
        """HMAC of body in the configured header, prefixed 'sha256=' or 'sha1='
        (GitHub style) or bare hex. Constant-time compare."""
        header_name = cfg.get('signature_header', 'X-Hub-Signature-256')
        provided = get_header(header_name, '')
        if not provided:
            return False
        algo = cfg.get('signature_algo', 'sha256')
        hasher = hashlib.sha256 if algo == 'sha256' else hashlib.sha1
        expected = hmac.new(secret.encode('utf-8'), raw_body, hasher).hexdigest()
        provided = provided.strip()
        if '=' in provided:
            _, _, provided = provided.partition('=')
        try:
            return hmac.compare_digest(expected, provided.strip())
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _verify_slack(secret, raw_body, get_header):
        """Slack signs ``v0:<ts>:<body>`` with HMAC-SHA256, hex result in
        ``X-Slack-Signature`` as ``v0=<hex>``. Timestamp is in
        ``X-Slack-Request-Timestamp`` and must be within ±5 minutes to thwart
        offline replay of captured requests."""
        sig = (get_header('X-Slack-Signature', '') or '').strip()
        ts = (get_header('X-Slack-Request-Timestamp', '') or '').strip()
        if not sig.startswith('v0=') or not ts:
            return False
        try:
            ts_int = int(ts)
        except ValueError:
            return False
        if abs(time.time() - ts_int) > WebhookManager.TIMESTAMP_TOLERANCE:
            return False
        base = f'v0:{ts}:'.encode('utf-8') + raw_body
        expected = 'v0=' + hmac.new(secret.encode('utf-8'), base, hashlib.sha256).hexdigest()
        try:
            return hmac.compare_digest(expected, sig)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _verify_stripe(secret, raw_body, get_header):
        """Stripe signs ``<ts>.<body>`` with HMAC-SHA256. The header
        ``Stripe-Signature`` is a comma-separated list of ``k=v`` pairs:
        ``t=<unix>,v1=<hex>,v0=<hex>``. We accept any v1 that matches; if a
        request has multiple v1 entries (during a secret rotation window),
        Stripe sends both and the receiver should accept either."""
        header = get_header('Stripe-Signature', '') or ''
        if not header:
            return False
        pairs = {}
        v1s = []
        for part in header.split(','):
            if '=' not in part:
                continue
            k, _, v = part.partition('=')
            k, v = k.strip(), v.strip()
            if k == 'v1':
                v1s.append(v)
            else:
                pairs[k] = v
        ts = pairs.get('t')
        if not ts or not v1s:
            return False
        try:
            ts_int = int(ts)
        except ValueError:
            return False
        if abs(time.time() - ts_int) > WebhookManager.TIMESTAMP_TOLERANCE:
            return False
        base = f'{ts}.'.encode('utf-8') + raw_body
        expected = hmac.new(secret.encode('utf-8'), base, hashlib.sha256).hexdigest()
        return any(
            hmac.compare_digest(expected, v) for v in v1s
        )

    @staticmethod
    def render_prompt(cfg, payload):
        """Apply the prompt template to the inbound payload.

        Two modes, chosen by the config:
          * 'attach' (default, safe): prompt = template + fenced JSON of payload.
            No interpolation, so payload contents can't smuggle instructions
            into the rendered prompt — they appear as data in a code fence.
          * 'interpolate': substitute {{ payload.x.y }} references with the
            matching JSON value. Caller-controlled values land verbatim in the
            instruction line — only use when the payload source is trusted.
        """
        template = cfg.get('prompt_template', '')
        mode = cfg.get('interpolate_mode', 'attach')
        if mode == 'interpolate':
            return WebhookManager._INTERP_RE.sub(
                lambda m: WebhookManager._lookup(payload, m.group(1)),
                template,
            )
        # attach mode
        try:
            pretty = json.dumps(payload, indent=2, default=str)
        except (TypeError, ValueError):
            pretty = repr(payload)
        return f'{template}\n\nWebhook payload:\n```json\n{pretty}\n```'

    @staticmethod
    def _lookup(payload, dotted):
        """Resolve a '.a.b.c' path against payload (dict-only). Returns '' if
        any segment is missing or the payload isn't traversable. Stringifies
        non-string leaves so the substitution always produces a string."""
        cur = payload
        # dotted is e.g. ".pull_request.title" — leading dot, may be empty
        parts = [p for p in dotted.split('.') if p]
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return ''
        if cur is None:
            return ''
        if isinstance(cur, (str, int, float, bool)):
            return str(cur)
        try:
            return json.dumps(cur, default=str)
        except (TypeError, ValueError):
            return ''


class CronManager:
    """Scheduled triggers backed by Kubernetes CronJob objects.

    Each cron has TWO pieces of state:
      * Local config JSON at /home/dev/.claude-triggers/crons/<id>.json
        (prompt_template, payload, response_url, fire_token, …)
      * A Kubernetes CronJob named cron-<user>-<id> + a matching Secret
        cron-<user>-<id>-token with the bearer the CronJob uses to call back.

    Why CronJob rather than an in-pod scheduler thread:
      * Native suspend/resume via `spec.suspend`
      * Native run-history via successful/failedJobsHistoryLimit
      * `kubectl get cronjobs -n coder` lists everything
      * Schedule fires even if the IDE pod is briefly down (the CronJob's
        curl will retry per the Job's backoffLimit)

    The CronJob's container is just curlimages/curl POSTing to the workspace
    service. The IDE pod is the actual executor; the CronJob is the timer.
    """

    CRONS_DIR = '/home/dev/.claude-triggers/crons'
    NAMESPACE_FILE = '/var/run/secrets/kubernetes.io/serviceaccount/namespace'
    _ID_RE = re.compile(r'^[a-z0-9-]{1,40}$')  # tighter than webhooks because used in k8s names
    # Cron schedule: 5 space-separated fields restricted to characters that
    # appear in real cron expressions (digits, *, /, -, ,). Restricting the
    # character class — vs. \S+ — closes off YAML injection via quote chars,
    # since the schedule is interpolated into the kubectl-apply manifest.
    _CRON_FIELD = r'[0-9*/,-]+'
    _SCHEDULE_RE = re.compile(
        r'^@(yearly|annually|monthly|weekly|daily|hourly)$|'
        r'^' + r'\s+'.join([_CRON_FIELD] * 5) + r'$')
    # IANA timezone names: letters, digits, _, /, +, -. Same anti-injection
    # reasoning as the schedule above.
    _TIMEZONE_RE = re.compile(r'^[A-Za-z0-9_/+\-]{1,64}$')

    @staticmethod
    def ensure_dir():
        os.makedirs(CronManager.CRONS_DIR, mode=0o700, exist_ok=True)

    @staticmethod
    def _config_path(cron_id):
        return os.path.join(CronManager.CRONS_DIR, f'{cron_id}.json')

    @staticmethod
    def valid_id(cron_id):
        return bool(cron_id) and bool(CronManager._ID_RE.match(cron_id))

    @staticmethod
    def detect_user():
        """Derive the workspace username from the pod hostname.
        Pods are named ws-<user>-<podhash>; the kaniko wrapper does the same.
        Falls back to env var WORKSPACE_USER if hostname doesn't match."""
        host = os.uname().nodename
        m = re.match(r'^ws-([a-z0-9-]+?)-[a-z0-9]+$', host)
        if m:
            return m.group(1)
        return os.environ.get('WORKSPACE_USER', 'unknown')

    @staticmethod
    def detect_namespace():
        try:
            with open(CronManager.NAMESPACE_FILE) as f:
                return f.read().strip()
        except OSError:
            return os.environ.get('POD_NAMESPACE', 'coder')

    @staticmethod
    def k8s_object_name(cron_id):
        """Stable name for both the CronJob and its companion Secret.
        Length-limited because k8s caps object names at 253 but Job names
        get a suffix appended at trigger time (~63 chars practical max)."""
        user = CronManager.detect_user()
        return f'cron-{user}-{cron_id}'[:50]

    @staticmethod
    def list_crons():
        CronManager.ensure_dir()
        out = []
        try:
            entries = sorted(os.listdir(CronManager.CRONS_DIR))
        except OSError:
            return out
        for name in entries:
            if not name.endswith('.json'):
                continue
            try:
                with open(os.path.join(CronManager.CRONS_DIR, name)) as f:
                    cfg = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            out.append(CronManager._public_view(cfg))
        return out

    @staticmethod
    def get_cron(cron_id, include_secrets=False):
        if not CronManager.valid_id(cron_id):
            return None
        try:
            with open(CronManager._config_path(cron_id)) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return cfg if include_secrets else CronManager._public_view(cfg)

    @staticmethod
    def _public_view(cfg):
        view = dict(cfg)
        for k in ('fire_token', 'response_secret'):
            if view.get(k):
                view[k + '_set'] = True
                view.pop(k)
        return view

    @staticmethod
    def create_or_update(data, existing_id=None):
        CronManager.ensure_dir()
        cron_id = existing_id or data.get('id', '')
        if not CronManager.valid_id(cron_id):
            return None, 'invalid id (1-40 chars, [a-z0-9-])'

        schedule = (data.get('schedule') or '').strip()
        if not CronManager._SCHEDULE_RE.match(schedule):
            return None, 'invalid schedule (5-field cron or @daily/@hourly/etc)'

        timezone = (data.get('timezone') or 'UTC').strip()
        if not CronManager._TIMEZONE_RE.match(timezone):
            return None, 'invalid timezone (IANA name like UTC or America/Los_Angeles)'

        prompt_template = (data.get('prompt_template') or '').strip()
        if not prompt_template:
            return None, 'prompt_template is required'

        mode = data.get('interpolate_mode', 'attach')
        if mode not in ('attach', 'interpolate'):
            return None, "interpolate_mode must be 'attach' or 'interpolate'"

        payload = data.get('payload')
        if payload is not None and not isinstance(payload, (dict, list)):
            return None, 'payload must be a JSON object or array'

        response_url = data.get('response_url')
        if response_url and not ClaudeTaskManager._is_safe_response_url(response_url):
            return None, 'response_url must be http(s)'

        cfg = {
            'id': cron_id,
            'schedule': schedule,
            'prompt_template': prompt_template,
            'workdir': data.get('workdir') or '/home/dev',
            'payload': payload if payload is not None else {},
            'interpolate_mode': mode,
            'timezone': timezone,
            'suspended': bool(data.get('suspended', False)),
            'created_at': time.time(),
        }
        if data.get('response_url'):
            cfg['response_url'] = data['response_url']
        if data.get('response_secret'):
            cfg['response_secret'] = data['response_secret']

        # Preserve created_at + fire_token across update
        prior = None
        if existing_id:
            prior = CronManager.get_cron(existing_id, include_secrets=True) or {}
            if prior.get('created_at'):
                cfg['created_at'] = prior['created_at']
            if prior.get('fire_token'):
                cfg['fire_token'] = prior['fire_token']

        # Mint the fire_token on first create; the CronJob pod uses it to auth
        # back into the workspace service.
        if not cfg.get('fire_token'):
            cfg['fire_token'] = secrets.token_urlsafe(32)

        # Persist local config
        path = CronManager._config_path(cron_id)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)

        # Apply (or re-apply) the K8s CronJob + Secret. If this fails, the
        # local config still lives — the user can re-apply by editing.
        try:
            CronManager._apply_k8s(cfg)
        except Exception as e:
            return cfg, f'config saved but kubectl apply failed: {e}'

        return cfg, None

    @staticmethod
    def delete(cron_id):
        if not CronManager.valid_id(cron_id):
            return False
        # Best-effort: tear down k8s objects even if local config is gone
        name = CronManager.k8s_object_name(cron_id)
        ns = CronManager.detect_namespace()
        for kind in ('cronjob', 'secret'):
            subprocess.run(
                ['kubectl', 'delete', kind, name, '-n', ns, '--ignore-not-found'],
                capture_output=True, text=True, timeout=30,
            )
        try:
            os.remove(CronManager._config_path(cron_id))
            return True
        except FileNotFoundError:
            # Still report success if we cleaned up k8s objects above
            return False

    @staticmethod
    def set_suspended(cron_id, suspended):
        cfg = CronManager.get_cron(cron_id, include_secrets=True)
        if cfg is None:
            return None
        cfg['suspended'] = bool(suspended)
        path = CronManager._config_path(cron_id)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)

        # Patch the CronJob's spec.suspend in place — cheaper than full re-apply.
        name = CronManager.k8s_object_name(cron_id)
        ns = CronManager.detect_namespace()
        patch = json.dumps({'spec': {'suspend': bool(suspended)}})
        subprocess.run(
            ['kubectl', 'patch', 'cronjob', name, '-n', ns, '--type=merge', '-p', patch],
            capture_output=True, text=True, timeout=30,
        )
        return cfg

    @staticmethod
    def rotate_token(cron_id):
        """Mint a fresh fire_token and re-apply the companion Secret.

        The CronJob references the Secret by name, so the next pod that
        spawns reads the new token. In-flight jobs that were already pulled
        from the API will fail their next call (intended — that's the
        rotation point). Returns (cfg, new_token) or (None, None) if the
        cron doesn't exist or the k8s apply failed (in which case the
        on-disk config is restored to the previous token to keep parity
        with the k8s Secret)."""
        cfg = CronManager.get_cron(cron_id, include_secrets=True)
        if cfg is None:
            return None, None
        # Remember the previous token so we can revert the on-disk config
        # if the k8s apply fails — without this the file would have the
        # new token while the Secret still has the old, and legitimate
        # CronJob fires would reject until the next successful rotation.
        old_token = cfg.get('fire_token')
        old_rotated_at = cfg.get('fire_token_rotated_at')
        new_token = secrets.token_urlsafe(32)
        cfg['fire_token'] = new_token
        cfg['fire_token_rotated_at'] = time.time()

        path = CronManager._config_path(cron_id)

        def _write_atomic(data):
            tmp = path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp, 0o600)
            os.rename(tmp, path)

        _write_atomic(cfg)

        try:
            CronManager._apply_k8s(cfg)
        except Exception as e:
            # Revert the on-disk config so the local file and the k8s Secret
            # agree on the same (old) token.
            cfg['fire_token'] = old_token
            if old_rotated_at is None:
                cfg.pop('fire_token_rotated_at', None)
            else:
                cfg['fire_token_rotated_at'] = old_rotated_at
            try:
                _write_atomic(cfg)
            except Exception as rollback_err:
                print(
                    f'[cron] rotate-token ROLLBACK FAILED for {cron_id}: {rollback_err}; '
                    f'disk has new token but k8s Secret still has the old one',
                    file=sys.stderr,
                )
            print(f'[cron] rotate-token kubectl apply failed for {cron_id}: {e}', file=sys.stderr)
            return None, None
        return cfg, new_token

    @staticmethod
    def run_now(cron_id):
        """Create a one-shot Job from the cron's CronJob — same effect as if
        the schedule had just fired."""
        if not CronManager.valid_id(cron_id):
            return False, 'invalid id'
        name = CronManager.k8s_object_name(cron_id)
        ns = CronManager.detect_namespace()
        # Suffix with timestamp so repeated 'run now' clicks don't collide
        job_name = f"{name}-manual-{int(time.time())}"[:50]
        r = subprocess.run(
            ['kubectl', 'create', 'job', job_name,
             '--from', f'cronjob/{name}', '-n', ns],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return False, r.stderr.strip() or 'kubectl create failed'
        return True, job_name

    @staticmethod
    def kubectl_status(cron_id):
        """Return k8s-side state for a cron: suspended flag, last-schedule-time,
        next-schedule-time. Returns {} if the CronJob isn't found or kubectl
        isn't available — never raises, so the dashboard stays usable."""
        if not CronManager.valid_id(cron_id):
            return {}
        name = CronManager.k8s_object_name(cron_id)
        ns = CronManager.detect_namespace()
        r = subprocess.run(
            ['kubectl', 'get', 'cronjob', name, '-n', ns, '-o', 'json'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return {}
        try:
            obj = json.loads(r.stdout)
        except json.JSONDecodeError:
            return {}
        spec = obj.get('spec', {}) or {}
        status = obj.get('status', {}) or {}
        return {
            'k8s_suspended': bool(spec.get('suspend', False)),
            'k8s_schedule': spec.get('schedule'),
            'k8s_last_schedule_time': status.get('lastScheduleTime'),
            'k8s_active': len(status.get('active', []) or []),
        }

    @staticmethod
    def render_prompt(cfg):
        """Cron's payload field plays the role of the inbound payload for
        webhooks — same rendering pipeline."""
        return WebhookManager.render_prompt(cfg, cfg.get('payload') or {})

    @staticmethod
    def _apply_k8s(cfg):
        """kubectl apply -f - for the Secret + CronJob. Raises on failure.
        Re-applies are safe (server-side merge semantics).

        The Secret holds the fire_token and is mounted as an env var into the
        curl pod. We intentionally do NOT pass the token via command-line args
        (would leak in `ps`) or via the URL (would leak in nginx access logs)."""
        name = CronManager.k8s_object_name(cfg['id'])
        ns = CronManager.detect_namespace()
        user = CronManager.detect_user()
        # base64 the token for the Secret (kubectl apply requires base64 for `data:`)
        token_b64 = base64.b64encode(cfg['fire_token'].encode('utf-8')).decode('ascii')

        # The receiver URL: in-cluster service DNS. Using cluster.local is the
        # safe default; if the cluster uses a different DNS suffix, override
        # via the WORKSPACE_INTERNAL_URL env var.
        internal_url = os.environ.get(
            'WORKSPACE_INTERNAL_URL',
            f'http://ws-{user}.{ns}.svc.cluster.local:6080',
        )

        manifest = f"""
apiVersion: v1
kind: Secret
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: kube-coder-cron
    workspace-user: {user}
    cron-id: {cfg['id']}
type: Opaque
data:
  token: {token_b64}
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: kube-coder-cron
    workspace-user: {user}
    cron-id: {cfg['id']}
spec:
  schedule: "{cfg['schedule']}"
  timeZone: "{cfg.get('timezone', 'UTC')}"
  suspend: {str(cfg.get('suspended', False)).lower()}
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 2
      ttlSecondsAfterFinished: 3600
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: trigger
            image: curlimages/curl:8.10.1
            command: ["/bin/sh", "-c"]
            args:
            - 'curl -fsS --max-time 30 -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{{}}" "{internal_url}/api/triggers/cron-fire/{cfg['id']}"'
            env:
            - name: TOKEN
              valueFrom:
                secretKeyRef:
                  name: {name}
                  key: token
"""
        r = subprocess.run(
            ['kubectl', 'apply', '-f', '-'],
            input=manifest, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or 'kubectl apply failed')

    @staticmethod
    def verify_fire_token(cron_id, provided):
        """Constant-time compare an inbound bearer token against the cron's
        fire_token. False on any mismatch or unknown id."""
        cfg = CronManager.get_cron(cron_id, include_secrets=True)
        if cfg is None:
            return False, None
        expected = cfg.get('fire_token') or ''
        if not provided or not expected:
            return False, None
        try:
            ok = hmac.compare_digest(expected, provided)
        except (TypeError, ValueError):
            ok = False
        return ok, cfg


class DesktopManager:
    """Backs the /api/desktop endpoints — the customizable launcher grid on
    the Desktop tab. Single JSON file at /home/dev/.kube-coder/desktop.json
    holds the full ordered icon list. One file (not one file per icon)
    keeps reordering trivial and avoids transient inconsistency on a
    cold pod read.

    Schema:
        {
          "version": 1,
          "items": [
            {
              "id":     "<8-hex>",
              "label":  "Refactor auth",
              "icon":   "📝",             # any single grapheme cluster
              "hotkey": "cmd+shift+1",     # optional
              "action": {
                "type":      "task",
                "prompt":    "...",
                "workdir":   "/home/dev/kube-coder",
                "assistant": "claude"      # or "opencode-openrouter" / "kc-harness"
              }
            }
          ]
        }

    Action types:
      task  — server creates a Claude/OpenCode task via ClaudeTaskManager
      url   — client opens in a new tab; server is just bookkeeping
      shell — server runs a one-shot bash command, returns stdout/stderr

    All shell commands run as the workspace user with the workspace's own
    environment + cwd — no privilege escalation, no setuid. The owner of
    the workspace also owns the desktop config so anyone who can edit a
    `shell` action can already run arbitrary commands via the terminal.
    """

    CONFIG_PATH = '/home/dev/.kube-coder/desktop.json'
    CONFIG_DIR = '/home/dev/.kube-coder'
    SHELL_TIMEOUT_DEFAULT = 30   # seconds
    SHELL_TIMEOUT_MAX = 300
    _ID_RE = re.compile(r'^[a-z0-9]{4,16}$')
    _ALLOWED_ACTION_TYPES = ('task', 'url', 'shell')

    @staticmethod
    def _ensure_dir():
        os.makedirs(DesktopManager.CONFIG_DIR, mode=0o755, exist_ok=True)

    # Seed icons rendered the first time a workspace opens the Desktop
    # tab. Each user can delete or edit any of these; the seed only fires
    # when desktop.json doesn't exist (first-ever load on the PVC).
    _SEED_ITEMS = [
        {
            'id': 'seedclaud',
            'label': 'New build',
            'icon': 'icon:chat',
            'hotkey': 'cmd+shift+c',
            'action': {
                'type': 'task',
                'prompt': '',
                'workdir': '/home/dev',
                'assistant': 'claude',
            },
        },
        {
            'id': 'seedbuild',
            'label': 'Builds',
            'icon': 'icon:tasks',
            'action': {
                'type': 'url',
                'url': '/tasks',
                'target': 'self',
            },
        },
        {
            'id': 'seedmem01',
            'label': 'Memory',
            'icon': 'icon:memory',
            'action': {
                'type': 'url',
                'url': '/memory',
                'target': 'self',
            },
        },
        {
            'id': 'seedsett1',
            'label': 'Settings',
            'icon': 'icon:settings',
            'action': {
                'type': 'url',
                'url': '/settings',
                'target': 'self',
            },
        },
    ]

    @staticmethod
    def _load_all():
        DesktopManager._ensure_dir()
        if not os.path.exists(DesktopManager.CONFIG_PATH):
            # First-ever load on this PVC — seed defaults so the Desktop
            # tab isn't an empty page on a fresh workspace. The user can
            # delete/edit/reorder them like any other icon.
            seeded = {'version': 1, 'items': list(DesktopManager._SEED_ITEMS)}
            try:
                DesktopManager._save_all(seeded)
            except OSError:
                pass  # If we can't write, just return the in-memory copy.
            return seeded
        try:
            with open(DesktopManager.CONFIG_PATH) as f:
                data = json.load(f)
            if not isinstance(data, dict) or 'items' not in data:
                return {'version': 1, 'items': []}
            if not isinstance(data['items'], list):
                data['items'] = []
            return data
        except (OSError, json.JSONDecodeError):
            return {'version': 1, 'items': []}

    @staticmethod
    def _save_all(data):
        DesktopManager._ensure_dir()
        # Atomic write — tmp + rename — so a crashed write can't leave the
        # config file empty / half-written and brick the Desktop route.
        tmp = DesktopManager.CONFIG_PATH + f'.tmp.{os.getpid()}'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DesktopManager.CONFIG_PATH)

    @staticmethod
    def _new_id():
        return secrets.token_hex(4)

    @staticmethod
    def _validate(item):
        """Validate an item submitted by the client. Returns the cleaned
        dict or raises ValueError. Server is the trust boundary; the SPA
        validates too but a curl client can post anything."""
        if not isinstance(item, dict):
            raise ValueError('item must be an object')
        label = str(item.get('label', '')).strip()
        if not label or len(label) > 80:
            raise ValueError('label must be 1-80 chars')
        icon = str(item.get('icon', '')).strip()
        # Accept either a short emoji/text (≤8 chars) or the named-icon
        # prefix form "icon:NAME" (up to 32 chars) which the SPA renders
        # via its built-in Icon component for the clean line-icon look.
        if not icon or len(icon) > 32:
            raise ValueError('icon must be 1-32 chars (emoji or "icon:NAME")')
        hotkey = item.get('hotkey')
        if hotkey is not None:
            hotkey = str(hotkey).strip().lower()
            if hotkey and not re.match(r'^[a-z0-9+\- ]{1,40}$', hotkey):
                raise ValueError('hotkey must be a short modifier expression e.g. "cmd+shift+1"')
            if not hotkey:
                hotkey = None
        action = item.get('action')
        if not isinstance(action, dict):
            raise ValueError('action must be an object')
        action_type = action.get('type')
        if action_type not in DesktopManager._ALLOWED_ACTION_TYPES:
            raise ValueError(f'action.type must be one of {DesktopManager._ALLOWED_ACTION_TYPES}')
        cleaned_action = {'type': action_type}
        if action_type == 'task':
            # Empty prompt is intentional — boots the assistant CLI into
            # interactive REPL mode (NewTaskForm sends '' too for the
            # "open a Claude session" flow). Just cap the upper bound.
            prompt = str(action.get('prompt', ''))
            if len(prompt) > 8000:
                raise ValueError('action.prompt must be <= 8000 chars')
            cleaned_action['prompt'] = prompt
            workdir = str(action.get('workdir', '')).strip() or '/home/dev'
            cleaned_action['workdir'] = workdir
            assistant = action.get('assistant')
            if assistant:
                cleaned_action['assistant'] = str(assistant).strip()
        elif action_type == 'url':
            url = str(action.get('url', '')).strip()
            if not url or not re.match(r'^(https?://|/)[\S]+$', url):
                raise ValueError('action.url must be http(s) or an absolute path')
            cleaned_action['url'] = url
            target = str(action.get('target', 'blank')).strip()
            if target not in ('blank', 'self'):
                raise ValueError('action.target must be "blank" or "self"')
            cleaned_action['target'] = target
        elif action_type == 'shell':
            command = str(action.get('command', '')).strip()
            if not command or len(command) > 4000:
                raise ValueError('action.command must be 1-4000 chars')
            cleaned_action['command'] = command
            try:
                timeout = int(action.get('timeout') or DesktopManager.SHELL_TIMEOUT_DEFAULT)
            except (TypeError, ValueError):
                raise ValueError('action.timeout must be an integer (seconds)')
            if timeout < 1 or timeout > DesktopManager.SHELL_TIMEOUT_MAX:
                raise ValueError(f'action.timeout must be 1-{DesktopManager.SHELL_TIMEOUT_MAX}s')
            cleaned_action['timeout'] = timeout
        cleaned = {
            'label': label,
            'icon': icon,
            'action': cleaned_action,
        }
        if hotkey:
            cleaned['hotkey'] = hotkey
        return cleaned

    @staticmethod
    def list_items():
        return DesktopManager._load_all().get('items', [])

    @staticmethod
    def create(item):
        cleaned = DesktopManager._validate(item)
        cleaned['id'] = DesktopManager._new_id()
        data = DesktopManager._load_all()
        data['items'].append(cleaned)
        DesktopManager._save_all(data)
        return cleaned

    @staticmethod
    def update(item_id, item):
        if not DesktopManager._ID_RE.match(item_id or ''):
            raise ValueError('invalid id')
        cleaned = DesktopManager._validate(item)
        cleaned['id'] = item_id
        data = DesktopManager._load_all()
        for i, existing in enumerate(data['items']):
            if existing.get('id') == item_id:
                data['items'][i] = cleaned
                DesktopManager._save_all(data)
                return cleaned
        raise ValueError('item not found')

    @staticmethod
    def delete(item_id):
        if not DesktopManager._ID_RE.match(item_id or ''):
            raise ValueError('invalid id')
        data = DesktopManager._load_all()
        before = len(data['items'])
        data['items'] = [it for it in data['items'] if it.get('id') != item_id]
        if len(data['items']) == before:
            raise ValueError('item not found')
        DesktopManager._save_all(data)

    @staticmethod
    def reorder(ordered_ids):
        if not isinstance(ordered_ids, list):
            raise ValueError('order must be an array of ids')
        data = DesktopManager._load_all()
        by_id = {it.get('id'): it for it in data['items']}
        new_items = []
        seen = set()
        for item_id in ordered_ids:
            if item_id in by_id and item_id not in seen:
                new_items.append(by_id[item_id])
                seen.add(item_id)
        # Append any items not mentioned (defensive — client should send all).
        for it in data['items']:
            if it.get('id') not in seen:
                new_items.append(it)
        data['items'] = new_items
        DesktopManager._save_all(data)
        return data['items']

    @staticmethod
    def get(item_id):
        for it in DesktopManager.list_items():
            if it.get('id') == item_id:
                return it
        return None


class DocsManager:
    """Backs the /api/docs endpoints used by the in-app documentation site.

    Source of truth is /home/dev/kube-coder/docs (cloned into every pod by
    start.sh, see CLAUDE.md). The manifest at docs/_manifest.json declares
    the nav tree; pages are plain markdown files. Per-file content is
    mtime-cached so polling clients don't re-read disk every request.
    """

    DOCS_DIR = os.environ.get(
        'DOCS_DIR', '/home/dev/kube-coder/docs'
    )
    # (path → (mtime, decoded_text)). Small enough that we never bother evicting.
    _PAGE_CACHE: dict = {}
    # Manifest is similarly mtime-cached.
    _MANIFEST_CACHE: tuple = (0.0, None)

    @classmethod
    def _safe_join(cls, rel: str) -> str:
        """Resolve `rel` under DOCS_DIR; raise on traversal."""
        rel = (rel or '').lstrip('/')
        # Reject backslashes and absolute drives outright — defensive only.
        if '\x00' in rel or rel.startswith('..'):
            raise ValueError('invalid path')
        base = os.path.realpath(cls.DOCS_DIR)
        target = os.path.realpath(os.path.join(base, rel))
        if target != base and not target.startswith(base + os.sep):
            raise ValueError('path escapes docs root')
        return target

    @classmethod
    def load_manifest(cls) -> dict:
        path = cls._safe_join('_manifest.json')
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {'version': 1, 'sections': []}
        cached_mtime, cached = cls._MANIFEST_CACHE
        if cached and cached_mtime == mtime:
            return cached
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        cls._MANIFEST_CACHE = (mtime, data)
        return data

    @classmethod
    def index(cls) -> dict:
        """Return the manifest plus a flat id→{title,file,summary,breadcrumbs} map."""
        manifest = cls.load_manifest()
        flat = {}
        for sec in manifest.get('sections', []):
            for page in sec.get('pages', []):
                flat[page['id']] = {
                    'id': page['id'],
                    'title': page.get('title', page['id']),
                    'file': page.get('file', ''),
                    'summary': page.get('summary', ''),
                    'section_id': sec.get('id'),
                    'section_title': sec.get('title'),
                }
        return {'manifest': manifest, 'pages': flat}

    @classmethod
    def get_page(cls, page_id: str) -> dict:
        index = cls.index()
        meta = index['pages'].get(page_id)
        if not meta:
            raise KeyError(page_id)
        path = cls._safe_join(meta['file'])
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            raise KeyError(page_id)
        cached = cls._PAGE_CACHE.get(path)
        if cached and cached[0] == mtime:
            markdown = cached[1]
        else:
            with open(path, 'r', encoding='utf-8') as f:
                markdown = f.read()
            cls._PAGE_CACHE[path] = (mtime, markdown)
        return {
            'id': meta['id'],
            'title': meta['title'],
            'summary': meta['summary'],
            'section_id': meta['section_id'],
            'section_title': meta['section_title'],
            'file': meta['file'],
            'edited_at': mtime,
            'markdown': markdown,
        }

    @classmethod
    def search(cls, q: str, limit: int = 25) -> list:
        """Substring + title-weighted search across all pages. Returns
        [{id,title,snippet,score}] sorted by score desc. Cheap O(N*M) scan
        — adequate for ~20 docs. Phase-6 work upgrades to SQLite FTS5."""
        needle = (q or '').strip().lower()
        if not needle:
            return []
        results = []
        index = cls.index()
        for page_id in index['pages']:
            try:
                page = cls.get_page(page_id)
            except KeyError:
                continue
            title_l = page['title'].lower()
            body_l = page['markdown'].lower()
            score = 0
            if needle in title_l:
                score += 10
            count = body_l.count(needle)
            score += min(count, 20)  # diminishing returns
            if score == 0:
                continue
            idx = body_l.find(needle)
            start = max(0, idx - 60)
            end = min(len(page['markdown']), idx + len(needle) + 80)
            snippet = page['markdown'][start:end].strip()
            results.append({
                'id': page['id'],
                'title': page['title'],
                'section_title': page['section_title'],
                'snippet': snippet,
                'score': score,
            })
        results.sort(key=lambda r: r['score'], reverse=True)
        return results[:limit]


class AppsManager:
    """Backs the Applications page in the dashboard SPA.

    Discovers locally-listening TCP services (from /proc/net/tcp[6]) and
    merges them with a user-curated list of "pinned" ports persisted on
    the workspace PVC. A pinned port gives the user a friendly name and
    stays in the list even when the underlying process is stopped, so the
    UI can show "my Django app — stopped" instead of an entry that
    disappears every time the server restarts.

    The proxy itself (BrowserHandler._proxy_app_request) calls is_proxyable
    to confirm the requested port is currently listening on loopback before
    forwarding — that prevents bearer-authed callers from probing arbitrary
    pod-external ports through the dashboard.
    """

    PINS_PATH = os.path.expanduser('~/.claude-tasks/apps.json')

    # Ports the workspace itself owns. Hidden from the auto-list and
    # refused by the proxy even if the user tries to pin them.
    INTERNAL_PORTS = frozenset({22, 2376, 5900, 6080, 6081, 7681, 8080})

    # Bind addresses we accept as "on loopback". 0.0.0.0 / :: are
    # "all interfaces" which includes loopback, so anything bound that
    # way is reachable from the pod and safe to proxy.
    LOOPBACK_ADDRS = frozenset({'127.0.0.1', '::1', '0.0.0.0', '::'})

    _NAME_RE = re.compile(r'^[\w \-./@:]{1,80}$')

    # --- /proc/net/tcp parsing ---

    @staticmethod
    def parse_listen_ports(tcp_path='/proc/net/tcp', tcp6_path='/proc/net/tcp6'):
        """Return [{port, addr, inode}] for every LISTEN socket bound to a
        loopback address. Parameters allow injecting fixture files in tests."""
        out = []
        for path, family in ((tcp_path, 4), (tcp6_path, 6)):
            try:
                with open(path) as f:
                    lines = f.read().splitlines()[1:]
            except (FileNotFoundError, PermissionError):
                continue
            for line in lines:
                parts = line.split()
                if len(parts) < 10 or parts[3] != '0A':  # 0A = TCP_LISTEN
                    continue
                local = parts[1]
                if ':' not in local:
                    continue
                ip_hex, port_hex = local.rsplit(':', 1)
                try:
                    port = int(port_hex, 16)
                except ValueError:
                    continue
                if family == 4:
                    addr = AppsManager._decode_ipv4_hex(ip_hex)
                else:
                    addr = AppsManager._decode_ipv6_hex(ip_hex)
                if addr is None or not AppsManager._is_loopback(addr):
                    continue
                try:
                    inode = int(parts[9])
                except ValueError:
                    inode = 0
                out.append({'port': port, 'addr': addr, 'inode': inode})
        # Dedupe by port (a service bound on both v4 and v6 shows up twice).
        seen = {}
        for entry in out:
            seen.setdefault(entry['port'], entry)
        return list(seen.values())

    @staticmethod
    def _decode_ipv4_hex(s):
        if len(s) != 8:
            return None
        try:
            return '.'.join(str(int(s[i:i + 2], 16)) for i in (6, 4, 2, 0))
        except ValueError:
            return None

    @staticmethod
    def _decode_ipv6_hex(s):
        """/proc/net/tcp6 IPv6: 32 hex chars, little-endian per 32-bit word.
        Decode to a canonical lowercase form so the loopback check matches."""
        if len(s) != 32:
            return None
        try:
            groups = []
            for i in range(0, 32, 8):
                word = s[i:i + 8]
                # Reverse bytes within the 32-bit word.
                be = word[6:8] + word[4:6] + word[2:4] + word[0:2]
                groups.append(be[:4].lower())
                groups.append(be[4:8].lower())
            full = ':'.join(groups)
        except ValueError:
            return None
        # Canonicalize the addresses we care about; leave others as-is.
        if full == '0000:0000:0000:0000:0000:0000:0000:0000':
            return '::'
        if full == '0000:0000:0000:0000:0000:0000:0000:0001':
            return '::1'
        # IPv4-mapped (::ffff:a.b.c.d) — present the dotted form so the
        # loopback check below can match the v4 string directly.
        if full.startswith('0000:0000:0000:0000:0000:ffff:'):
            tail = full.split(':')[-2:]  # ['7f00', '0001']
            try:
                packed = int(tail[0] + tail[1], 16).to_bytes(4, 'big')
                return f'::ffff:{packed[0]}.{packed[1]}.{packed[2]}.{packed[3]}'
            except ValueError:
                return full
        return full

    @staticmethod
    def _is_loopback(addr):
        if addr in AppsManager.LOOPBACK_ADDRS:
            return True
        # IPv4-mapped IPv6 loopback (::ffff:127.0.0.1).
        if addr.startswith('::ffff:') and addr.endswith('.127.0.0.1'):
            return True
        if addr.startswith('::ffff:127.'):
            return True
        return False

    # --- pin persistence ---

    @staticmethod
    def _ensure_dir():
        os.makedirs(os.path.dirname(AppsManager.PINS_PATH), mode=0o700, exist_ok=True)

    @staticmethod
    def _load_pins():
        if not os.path.exists(AppsManager.PINS_PATH):
            return {}
        try:
            with open(AppsManager.PINS_PATH) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in data.items():
            if not isinstance(v, dict):
                continue
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _save_pins(pins):
        AppsManager._ensure_dir()
        # JSON keys must be strings; sort for stable diffs.
        on_disk = {str(p): pins[p] for p in sorted(pins.keys())}
        tmp = AppsManager.PINS_PATH + f'.tmp.{os.getpid()}'
        with open(tmp, 'w') as f:
            json.dump(on_disk, f, indent=2)
        os.replace(tmp, AppsManager.PINS_PATH)

    @staticmethod
    def _validate_port(port):
        try:
            p = int(port)
        except (TypeError, ValueError):
            raise ValueError('port must be an integer')
        if not (1 <= p <= 65535):
            raise ValueError('port must be between 1 and 65535')
        return p

    @staticmethod
    def _validate_name(name):
        s = str(name or '').strip()
        if not s:
            raise ValueError('name is required')
        if not AppsManager._NAME_RE.match(s):
            raise ValueError(
                'name must be 1-80 chars; letters, digits, space, _-./@: only'
            )
        return s

    @classmethod
    def add_pin(cls, port, name, strip_prefix=False):
        port = cls._validate_port(port)
        name = cls._validate_name(name)
        pins = cls._load_pins()
        pins[port] = {
            'name': name,
            'strip_prefix': bool(strip_prefix),
            'created_at': time.time(),
        }
        cls._save_pins(pins)
        return pins[port]

    @classmethod
    def remove_pin(cls, port):
        port = cls._validate_port(port)
        pins = cls._load_pins()
        if port in pins:
            del pins[port]
            cls._save_pins(pins)
            return True
        return False

    @classmethod
    def get_pin(cls, port):
        try:
            port = cls._validate_port(port)
        except ValueError:
            return None
        return cls._load_pins().get(port)

    # --- merged view ---

    @classmethod
    def list_apps(cls):
        """Merged list shown on the Applications page.

        Order: pinned entries first (sorted by name), then discovered
        entries that aren't pinned (sorted by port).
        """
        listeners = {entry['port']: entry for entry in cls.parse_listen_ports()}
        pins = cls._load_pins()
        rows = []
        seen = set()
        for port in sorted(pins.keys(), key=lambda p: (pins[p].get('name', '').lower(), p)):
            seen.add(port)
            pin = pins[port]
            listening = port in listeners
            if port in cls.INTERNAL_PORTS:
                rows.append({
                    'port': port, 'name': pin.get('name', ''),
                    'pinned': True, 'status': 'blocked',
                    'strip_prefix': bool(pin.get('strip_prefix')),
                    'addr': listeners.get(port, {}).get('addr', ''),
                })
                continue
            rows.append({
                'port': port, 'name': pin.get('name', ''),
                'pinned': True,
                'status': 'running' if listening else 'stopped',
                'strip_prefix': bool(pin.get('strip_prefix')),
                'addr': listeners.get(port, {}).get('addr', ''),
            })
        for port in sorted(listeners.keys()):
            if port in seen or port in cls.INTERNAL_PORTS:
                continue
            rows.append({
                'port': port, 'name': '',
                'pinned': False, 'status': 'running',
                'strip_prefix': False,
                'addr': listeners[port].get('addr', ''),
            })
        return rows

    @classmethod
    def is_proxyable(cls, port):
        """(ok, reason). Called by the proxy before forwarding."""
        try:
            port = cls._validate_port(port)
        except ValueError as e:
            return False, str(e)
        if port in cls.INTERNAL_PORTS:
            return False, f'port {port} is reserved for the workspace'
        listeners = {e['port']: e for e in cls.parse_listen_ports()}
        if port not in listeners:
            return False, f'port {port} is not currently listening on loopback'
        return True, ''


class BrowserHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Force browsers (especially mobile Safari) to revalidate the
        # dashboard on each visit. Without this, SimpleHTTPRequestHandler
        # sends no Cache-Control and Safari can pin a stale SPA index.html
        # for days, hiding bundle updates behind a manual cache-clear.
        # Applied to HTML AND to SPA routes (which don't end in .html but
        # serve index.html via serve_next_spa) — the previous .html-only
        # check missed /, /memory, /tasks etc., leading to users stuck on
        # months-old bundles. Static hashed assets keep default heuristics.
        path = (self.path or '').split('?', 1)[0].lower()
        is_html = path.endswith('.html')
        is_spa_route = (
            path in ('/', '/dashboard', '/dashboard/', '/browser', '/browser/', '/next', '/next/')
            or path.startswith('/next/')
            or any(path == r or path.startswith(r + '/')
                   for r in ('/tasks', '/memory', '/apps', '/triggers', '/files', '/docs', '/settings'))
        )
        if is_html or is_spa_route:
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
            self.send_header('Pragma', 'no-cache')
        super().end_headers()

    def do_GET(self):
        # Normalize path: strip /oauth and /browser prefixes from ingress
        # rewrites, AND strip the query string before matching routes.
        # Without dropping the query string, deep-link URLs like
        # /oauth/?task=<id>&chat=open never match the "/" dashboard route
        # and fall through to the static-file 404.
        path_no_query = self.path.split('?', 1)[0]
        normalized_path = path_no_query.replace('/oauth', '').replace('/browser', '')
        if normalized_path == '' or normalized_path == '/':
            normalized_path = '/'

        # Sub-resources an embedded app loaded that escaped the proxy prefix
        # (lazy route chunks, @font-face fonts, …) land at the dashboard root.
        # If the Referer is an app-proxy iframe, send them back to that app.
        # Runs before dashboard routing so an escaped /tasks etc. goes to the
        # app rather than serving it the dashboard SPA.
        if self._dispatch_referer_proxy('GET'):
            return

        # All SPA routes serve the new dashboard. /next/* is the explicit form
        # (kept for backward compat after cutover) and the bare top-level
        # routes (/, /tasks, /memory, …) all serve the same SPA index.html so
        # client-side routing handles deep links. The legacy dashboard.html
        # has been removed; if /opt/dashboard-dist is missing we return 503
        # rather than fall back to anything stale.
        SPA_TOP_LEVEL = {'/', '/tasks', '/memory', '/apps', '/triggers', '/files', '/docs', '/settings', '/desktop'}
        first_seg = '/' + normalized_path.split('/')[1] if normalized_path != '/' else '/'
        if normalized_path == "/next" or normalized_path == "/next/" or normalized_path.startswith("/next/"):
            rel = normalized_path[len("/next"):] if normalized_path.startswith("/next") else ""
            self.serve_next_spa(rel)
            return
        elif (
            normalized_path in ["/", "/dashboard", "/dashboard/"]
            or normalized_path in ["/browser", "/browser/"]
            or first_seg in SPA_TOP_LEVEL
        ):
            # SPA at root. /dashboard and /browser kept for back-compat URLs.
            self.serve_next_spa('/')
            return
        elif self.path == "/livez":
            self.send_livez()
            return
        elif self.path == "/health":
            self.send_health_check()
            return
        elif self.path == "/health/vscode":
            self.send_vscode_health()
            return
        elif self.path == "/health/terminal":
            self.send_terminal_health()
            return
        elif self.path == "/health/browser":
            self.send_browser_health()
            return
        elif self.path == "/metrics":
            self.send_metrics()
            return
        elif self.path == "/api/github/status":
            self.send_github_status()
            return
        elif self.path == "/api/github/config":
            self.send_git_config()
            return
        elif self.path == "/vnc" or self.path == "/vnc/":
            self.send_vnc_viewer()
            return
        elif self.path == "/vnc-proxy" or self.path == "/vnc-proxy/":
            self.redirect_to_vnc()
            return
        elif self.path.startswith("/vnc/"):
            self.proxy_vnc_request()
            return

        # --- Claude Task API (GET) ---
        # Query string is already stripped at the top; handlers re-parse it from self.path when needed.
        claude_path = normalized_path
        # /api/mode — public deployment-mode probe used by the SPA at boot to
        # decide whether to hide mutation UI. Intentionally unauthenticated so
        # the read-only public demo can fetch it without an auth proxy in
        # front. Returns the two flags server.py was started with — never
        # derived per-request, never user-controllable.
        if claude_path == '/api/mode':
            self.send_json({
                'readOnly': READONLY_MODE,
                'authed': AUTH_MODE != 'none',
                'authMode': AUTH_MODE,
                'demoShowAll': DEMO_SHOW_ALL,
            })
            return
        # /api/apps — Applications page list endpoint.
        if claude_path == '/api/apps':
            self._handle_apps_list()
            return
        # Reverse-proxy to a locally-listening web app.
        if self._dispatch_app_proxy(claude_path, 'GET'):
            return
        if claude_path == '/api/claude/tasks':
            self.handle_claude_list_tasks()
            return
        elif claude_path == '/api/claude/auth/token':
            self.handle_claude_get_token()
            return
        elif claude_path == '/api/claude/assistants':
            self.handle_claude_list_assistants()
            return
        elif claude_path == '/api/workspace/dirs':
            self.handle_workspace_dirs()
            return

        # /api/claude/tasks/{id}/stream — Server-Sent Events
        m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/stream$', claude_path)
        if m:
            self._claude_task_id = m.group(1)
            self.handle_claude_stream_output()
            return
        # /api/claude/tasks/{id} and /api/claude/tasks/{id}/output
        m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/output$', claude_path)
        if m:
            self._claude_task_id = m.group(1)
            self.handle_claude_get_output()
            return
        m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)$', claude_path)
        if m:
            self._claude_task_id = m.group(1)
            self.handle_claude_get_task()
            return

        # --- Webhook CRUD (dashboard) ---
        if claude_path == '/api/webhooks':
            self.handle_webhook_list()
            return
        m = re.match(r'^/api/webhooks/([a-zA-Z0-9_-]+)$', claude_path)
        if m:
            self._webhook_id = m.group(1)
            self.handle_webhook_get()
            return

        # --- Cron CRUD (dashboard) ---
        if claude_path == '/api/crons':
            self.handle_cron_list()
            return
        m = re.match(r'^/api/crons/([a-z0-9-]+)$', claude_path)
        if m:
            self._cron_id = m.group(1)
            self.handle_cron_get()
            return

        # --- Desktop launcher (dashboard) ---
        if claude_path == '/api/desktop':
            self.handle_desktop_list()
            return
        m = re.match(r'^/api/desktop/([a-z0-9]+)$', claude_path)
        if m:
            item = DesktopManager.get(m.group(1))
            if item is None:
                self.send_json({'error': 'item not found'}, 404)
            else:
                self.send_json(item)
            return

        # --- Memory API (dashboard surface; backs the Memory tab) ---
        # Parse the query string once so list/search can use it.
        query_string = self.path.split('?', 1)[1] if '?' in self.path else ''
        memory_query = urllib.parse.parse_qs(query_string)
        if claude_path == '/api/memory':
            self.handle_memory_list(memory_query)
            return
        if claude_path == '/api/memory/stats':
            self.handle_memory_stats()
            return
        m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/history$', claude_path)
        if m:
            self.handle_memory_history(m.group(1), m.group(2))
            return
        m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/refs$', claude_path)
        if m:
            self.handle_memory_refs(m.group(1), m.group(2))
            return
        m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/neighbors$', claude_path)
        if m:
            self.handle_memory_neighbors(m.group(1), m.group(2), memory_query)
            return
        m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)$', claude_path)
        if m:
            self.handle_memory_get(m.group(1), m.group(2))
            return

        # --- Subagents (read-only view over Claude's transcripts) ---
        if claude_path == '/api/subagents':
            self.handle_subagents_list()
            return

        # --- Docs (in-app documentation site) ---
        if claude_path == '/api/docs':
            self.handle_docs_manifest()
            return
        if claude_path == '/api/docs/search':
            self.handle_docs_search(memory_query)
            return
        m = re.match(r'^/api/docs/([a-zA-Z0-9_-]+)$', claude_path)
        if m:
            self.handle_docs_page(m.group(1))
            return

        # --- File browser (lists /home/dev and child directories) ---
        if claude_path == '/api/files/list':
            self.handle_files_list()
            return

        super().do_GET()

    def serve_next_spa(self, rel_path):
        """Serve the new Preact SPA built into /opt/dashboard-dist/.

        rel_path is the path *after* /next (e.g. '' for /next, '/assets/x.js').
        SPA history fallback: if the path has no extension and the file is
        missing, fall back to index.html so client-routed deep links work
        after a refresh.

        DASHBOARD_DIST_DIR overrides the default location so tests + local
        dev can point at charts/workspace/web/dist.
        """
        import mimetypes
        base = os.environ.get('DASHBOARD_DIST_DIR') or '/opt/dashboard-dist'
        if not os.path.isdir(base):
            self.send_error(
                404,
                'New dashboard is not built. Run `yarn --cwd charts/workspace/web build` '
                'or set DASHBOARD_DIST_DIR to a built dist/ directory.',
            )
            return
        # Strip leading slash, decode percent-escapes, refuse traversal.
        rel = urllib.parse.unquote(rel_path).lstrip('/')
        if rel == '' or rel.endswith('/'):
            rel = 'index.html'
        target = os.path.normpath(os.path.join(base, rel))
        base_real = os.path.realpath(base)
        target_real = os.path.realpath(target)
        if not (target_real == base_real or target_real.startswith(base_real + os.sep)):
            self.send_error(403, 'Forbidden')
            return
        # History fallback for client-side routes: no extension + not found.
        if not os.path.isfile(target_real) and '.' not in os.path.basename(target_real):
            target_real = os.path.join(base_real, 'index.html')
            rel = 'index.html'
        if not os.path.isfile(target_real):
            self.send_error(404, 'Not found')
            return
        ctype, _ = mimetypes.guess_type(target_real)
        if ctype is None:
            ctype = 'application/octet-stream'
        try:
            with open(target_real, 'rb') as fh:
                body = fh.read()
        except OSError as exc:
            self.send_error(500, f'Read error: {exc}')
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        # Vite emits hashed filenames into /assets/, so those are safe to cache
        # for a year. index.html and other top-level files must revalidate so
        # deploys take effect on next request.
        if rel.startswith('assets/'):
            self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
        else:
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
        self.end_headers()
        self.wfile.write(body)

    def check_auth(self):
        """Legacy auth check used by the (deprecated) pre-SPA endpoints.
        Honors Remote-User only when TRUSTED_PROXY=true so a misconfigured
        ingress cannot be exploited by client-supplied headers."""
        if self.headers.get('Authorization', ''):
            return True
        if TRUSTED_PROXY and self.headers.get('Remote-User', ''):
            return True
        return False
    
    # --- Claude Task API helpers ---

    @staticmethod
    def _strip_route_prefix(path):
        """Strip the SPA's leading `/oauth/` and `/browser/` route prefixes.

        A naive `path.replace('/oauth', '').replace('/browser', '')` (which
        this method replaces) corrupts paths that contain the substrings
        mid-string — e.g. `/api/oauth/foo` becomes `/api//foo` and may not
        match any registered route. Only prefix matches are stripped, and
        the bare `/oauth` or `/browser` route maps to `/`. Order matters
        because both wraps are valid (`/oauth/browser/api/x` -> `/api/x`).
        """
        if path.startswith('/oauth/'):
            path = path[len('/oauth'):]
        elif path == '/oauth':
            path = '/'
        if path.startswith('/browser/'):
            path = path[len('/browser'):]
        elif path == '/browser':
            path = '/'
        return path

    def check_claude_auth(self, allow_none_mode=True):
        """Returns True if request is authenticated via OAuth2 headers OR valid bearer token.

        Short-circuits to True when AUTH_MODE=none — the public-demo
        deployment runs without an auth proxy in front of it. Guarded at
        startup (see _check_safety_invariants below) so this combo is only
        allowed when READONLY_MODE=true.

        Set allow_none_mode=False on endpoints that must always require a
        real identity (e.g. anything that returns PII or workspace secrets
        — the public demo must not leak the operator's git config / SSH
        key fingerprint just because the demo runs unauth'd).

        Upstream-auth headers (X-Auth-Request-*, Remote-User) are only
        honored when TRUSTED_PROXY=true — otherwise a misconfigured
        ingress that doesn't strip client-supplied headers becomes a
        trivial auth bypass.
        """
        if AUTH_MODE == 'none' and allow_none_mode:
            return True
        if TRUSTED_PROXY:
            if self.headers.get('X-Auth-Request-User') or self.headers.get('X-Auth-Request-Email'):
                return True
            if self.headers.get('Remote-User', ''):
                return True
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
            return ClaudeTaskManager.verify_token(token)
        return False

    def check_oauth_only(self):
        """Returns True only if request has OAuth2 proxy headers (not bearer token).
        Only honored when TRUSTED_PROXY=true; otherwise returns False."""
        if not TRUSTED_PROXY:
            return False
        if self.headers.get('X-Auth-Request-User') or self.headers.get('X-Auth-Request-Email'):
            return True
        if self.headers.get('Remote-User', ''):
            return True
        return False

    def send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self, max_bytes=None):
        """Read + parse a JSON request body, refusing anything over the cap.
        Without the cap a single Content-Length: big POST will OOM the pod.
        Raises ValueError on oversized bodies; handlers should treat the
        same way they treat JSONDecodeError (400)."""
        cap = max_bytes if max_bytes is not None else MAX_REQUEST_BODY_BYTES
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            return {}
        if content_length > cap:
            raise ValueError(f'request body too large ({content_length} > {cap})')
        body = self.rfile.read(content_length).decode('utf-8')
        return json.loads(body) if body else {}

    def _readonly_block(self):
        """Reject mutating requests when READONLY_MODE=true. Single chokepoint
        called at the top of do_POST/do_DELETE/do_PUT so individual handlers
        don't each need to remember to gate themselves."""
        if not READONLY_MODE:
            return False
        self.send_json({
            'error': 'This workspace is a read-only public demo. '
                     'Sign in to a personal workspace at https://github.com/imran31415/kube-coder '
                     'for full read-write access.',
            'code': 'readonly',
        }, 403)
        return True

    def do_DELETE(self):
        if self._readonly_block():
            return
        try:
            path = self._strip_route_prefix(self.path)
            # /api/apps/pins/<port> — remove a pinned port. Match before the
            # generic app-proxy dispatcher so the proxy doesn't swallow it.
            m = re.match(r'^/api/apps/pins/(\d+)$', path)
            if m:
                self._handle_apps_pin_delete(int(m.group(1)))
                return
            if self._dispatch_app_proxy(path, 'DELETE'):
                return
            m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)$', path)
            if m:
                self._claude_task_id = m.group(1)
                self.handle_claude_delete_task()
                return
            m = re.match(r'^/api/webhooks/([a-zA-Z0-9_-]+)$', path)
            if m:
                self._webhook_id = m.group(1)
                self.handle_webhook_delete()
                return
            m = re.match(r'^/api/crons/([a-z0-9-]+)$', path)
            if m:
                self._cron_id = m.group(1)
                self.handle_cron_delete()
                return
            m = re.match(r'^/api/desktop/([a-z0-9]+)$', path)
            if m:
                self.handle_desktop_delete(m.group(1))
                return
            m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/relations/(\d+)$', path)
            if m:
                self.handle_memory_unlink(m.group(1), m.group(2), int(m.group(3)))
                return
            m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)$', path)
            if m:
                self.handle_memory_delete(m.group(1), m.group(2))
                return
            self.send_json({'error': 'Not found'}, 404)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    # --- Claude Task API handlers ---

    def handle_claude_list_tasks(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Parse ?parent=<task_id> filter from query string
        parent = None
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            parent_val = params.get('parent', [None])[0]
            if parent_val:
                parent = parent_val
        tasks = ClaudeTaskManager.list_tasks(parent=parent)
        self.send_json({'tasks': tasks})

    def handle_workspace_dirs(self):
        """List candidate working directories under /home/dev for the
        new-task picker. Reuses Claude auth (OAuth header OR bearer token)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'dirs': WorkspaceManager.list_dirs()})

    def handle_claude_get_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        task = ClaudeTaskManager.get_task(self._claude_task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        self.send_json(task)

    def handle_claude_get_output(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Parse ?tail=N from query string
        tail = None
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            tail_val = params.get('tail', [None])[0]
            if tail_val and tail_val.isdigit():
                tail = int(tail_val)
        output = ClaudeTaskManager.get_task_output(self._claude_task_id, tail=tail)
        if output is None:
            self.send_json({'error': 'Task or output not found'}, 404)
            return
        # JSON-wrap the body so the SPA's typed fetch client (which expects
        # `{output: string}`) deserializes correctly. The legacy dashboard's
        # raw-text consumer was retired with the dashboard.html removal.
        self.send_json({'output': output})

    def handle_claude_stream_output(self):
        """Server-Sent Events stream of a task's rendered tmux output.

        Polls `tmux capture-pane` every ~1.5s and emits the diff vs the previous
        capture. We use capture-pane (not raw pipe-pane bytes) because claude-code
        is an interactive TUI: it emits cursor-moves, \\r-redraws, and spinner
        animations that look like garbage when streamed raw. tmux maintains the
        rendered screen state, so capture-pane gives us clean text.

        For completed tasks (tmux session gone) falls back to output.log.

        Query params:
          from=start (default) — send the current full capture once, then diffs.
          from=live            — skip initial capture, only send what changes after.
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return

        task_id = self._claude_task_id
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            self.send_json({'error': 'Task not found'}, 404)
            return

        from_param = 'start'
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            from_param = params.get('from', ['start'])[0]

        try:
            with open(meta_path, 'r') as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.send_json({'error': 'Task metadata unreadable'}, 500)
            return
        session_name = meta.get('tmux_session', f'claude-{task_id}')
        output_log = os.path.join(task_dir, 'output.log')

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        # Tell ingress-nginx not to buffer — otherwise SSE chunks won't flush in real time.
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        def write_raw(payload_bytes):
            try:
                self.wfile.write(payload_bytes)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def write_sse(data):
            # SSE framing: prefix every line with "data: ", terminate event with blank line.
            lines = data.split('\n')
            block = ''.join(f'data: {line}\n' for line in lines) + '\n'
            return write_raw(block.encode('utf-8'))

        def capture():
            """Return the current pane content (with history), or fall back to
            output.log if the tmux session is gone (task completed/killed)."""
            r = subprocess.run(
                ['tmux', 'capture-pane', '-J', '-t', session_name, '-p', '-S', '-2000'],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout:
                return strip_ansi(r.stdout).rstrip('\n') + '\n'
            if os.path.exists(output_log):
                try:
                    with open(output_log, 'r', errors='replace') as f:
                        return strip_ansi(f.read()).rstrip('\n') + '\n'
                except OSError:
                    pass
            return ''

        last_capture = ''
        if from_param == 'start':
            initial = capture()
            if initial:
                if not write_sse(initial):
                    return
                last_capture = initial
        else:
            # Live mode: prime last_capture so we don't replay history.
            last_capture = capture()

        last_heartbeat = time.time()
        last_change = time.time()
        started = time.time()
        poll_interval = 1.5
        heartbeat_interval = 15
        idle_grace = 5  # seconds after task stops with no diff before we close

        while True:
            # Hard cap so a client that never disconnects can't pin a
            # handler thread forever (combined with ThreadingHTTPServer's
            # unbounded thread spawn this is the easiest path to DoS).
            # Emit a graceful end event so the SPA reconnects cleanly.
            if time.time() - started > STREAM_MAX_SECONDS:
                write_raw(b'event: end\ndata: timeout\n\n')
                return
            new_capture = capture()
            if new_capture != last_capture:
                if new_capture.startswith(last_capture):
                    # Pure append — emit just the new tail.
                    diff = new_capture[len(last_capture):]
                elif last_capture.startswith(new_capture):
                    # Capture shrank (rare; pane cleared). Wait for next snapshot.
                    diff = ''
                else:
                    # History scrolled off / pane cleared. Send a marker plus the
                    # new content so the user sees something without a giant replay.
                    diff = '\n[output buffer rewound]\n' + new_capture
                last_capture = new_capture
                if diff:
                    if not write_sse(diff):
                        return
                    last_change = time.time()
                    last_heartbeat = last_change

            if time.time() - last_heartbeat > heartbeat_interval:
                if not write_raw(b': keep-alive\n\n'):
                    return
                last_heartbeat = time.time()

            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                ClaudeTaskManager._reconcile_status(meta, task_dir)
                status = meta.get('status', 'unknown')
            except (OSError, json.JSONDecodeError):
                status = 'unknown'

            if status not in ('running', 'waiting-for-input') and time.time() - last_change > idle_grace:
                write_raw(f'event: end\ndata: {status}\n\n'.encode('utf-8'))
                return

            time.sleep(poll_interval)

    def handle_claude_create_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        # Prompt is optional — the SPA's "Create build" flow drops the textarea
        # and spawns an interactive Claude/OpenCode session the user can type
        # into directly. Empty prompt → no-op Enter after the 3s assistant init.
        prompt = data.get('prompt', '').strip()
        workdir = data.get('workdir')
        response_url = data.get('response_url') or None
        response_secret = data.get('response_secret') or None
        source = data.get('source') or None
        disable_memory_injection = bool(data.get('disable_memory_injection'))
        assistant = data.get('assistant') or None
        if response_url and not ClaudeTaskManager._is_safe_response_url(response_url):
            self.send_json({'error': 'response_url must be http(s)'}, 400)
            return
        task = ClaudeTaskManager.create_task(
            prompt,
            workdir=workdir,
            response_url=response_url,
            response_secret=response_secret,
            source=source,
            disable_memory_injection=disable_memory_injection,
            assistant=assistant,
        )
        self.send_json(task, 201)

    def handle_claude_create_terminal_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Body is optional — accept {} or no body at all.
        workdir = None
        try:
            data = self.read_json_body()
            if isinstance(data, dict):
                workdir = data.get('workdir') or None
        except (json.JSONDecodeError, ValueError):
            pass
        task = ClaudeTaskManager.create_terminal_task(workdir=workdir)
        # Pre-arm the ttyd entry script so the next /oauth/terminal/ load
        # attaches to this session instead of dropping to a fresh bash.
        if task.get('status') != 'error':
            session_name = task.get('tmux_session', '')
            try:
                with open('/tmp/.claude-terminal-pending', 'w') as f:
                    f.write(session_name)
            except OSError:
                pass
        self.send_json(task, 201)

    def handle_claude_followup(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        prompt = data.get('prompt', '').strip()
        if not prompt:
            self.send_json({'error': 'prompt is required'}, 400)
            return
        task, err = ClaudeTaskManager.send_followup(self._claude_task_id, prompt)
        if task is None:
            self.send_json({'error': err or 'Task not found'}, 404)
            return
        self.send_json(task)

    def handle_claude_rename_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'Body must be a JSON object'}, 400)
            return
        meta, err = ClaudeTaskManager.rename_task(self._claude_task_id, data)
        if meta is None:
            status = 404 if err == 'not_found' else 400
            self.send_json({'error': err or 'Task not found'}, status)
            return
        self.send_json(meta)

    def handle_claude_delete_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        task = ClaudeTaskManager.delete_task(self._claude_task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        self.send_json(task)

    def handle_claude_get_token(self):
        if not self.check_oauth_only():
            self.send_json({'error': 'This endpoint requires OAuth2 authentication (browser session)'}, 401)
            return
        token = ClaudeTaskManager.get_or_create_token()
        self.send_json({'token': token})

    def handle_claude_list_assistants(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'assistants': ClaudeTaskManager.available_assistants()})

    def handle_claude_regenerate_token(self):
        if not self.check_oauth_only():
            self.send_json({'error': 'This endpoint requires OAuth2 authentication (browser session)'}, 401)
            return
        token = ClaudeTaskManager.regenerate_token()
        self.send_json({'token': token})

    def handle_claude_prepare_terminal(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        task_id = self._claude_task_id
        task = ClaudeTaskManager.get_task(task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        session_name = task.get('tmux_session', f'claude-{task_id}')
        # Wait up to ~3s for the tmux session to be visible to has-session
        # before declaring ourselves ready. Without this, the SPA can load
        # the iframe and run terminal-entry.sh while the session that
        # create_task spawned is still mid-registration; the entry script
        # falls through to bash and the user sees a fresh shell instead
        # of their task. Cheap on the happy path — has-session is ~1ms.
        deadline = time.time() + 3.0
        session_ready = False
        while time.time() < deadline:
            check = subprocess.run(
                ['tmux', 'has-session', '-t', session_name],
                capture_output=True,
            )
            if check.returncode == 0:
                session_ready = True
                break
            time.sleep(0.1)
        try:
            with open('/tmp/.claude-terminal-pending', 'w') as f:
                f.write(session_name)
            self.send_json({
                'ok': True,
                'session': session_name,
                'session_ready': session_ready,
            })
        except OSError as e:
            self.send_json({'error': str(e)}, 500)

    # --- Webhook handlers ---
    # CRUD endpoints (list/get/create/delete) reuse check_claude_auth — they
    # manage webhook *configs* and require the same trust as creating tasks.
    # The receiver endpoint (handle_webhook_receive) is intentionally NOT
    # behind that auth: external services authenticate via HMAC of the body.

    def handle_claude_scroll_mode(self):
        """Toggle tmux copy-mode for a task's pane.
        Replaces the user holding Ctrl+B [ to scroll and `q` to exit —
        instead the SPA shows a single Scroll-mode button that POSTs here.
        Once in copy-mode, arrow keys / Page Up / mouse wheel all navigate
        the scrollback (xterm.js's alt-screen wheel→arrow conversion lands
        on copy-mode's own arrow bindings, which is what we want here)."""
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        action = (data.get('action') or '').strip().lower()
        if action not in ('enter', 'exit'):
            self.send_json({'error': "action must be 'enter' or 'exit'"}, 400)
            return
        task_id = self._claude_task_id
        task = ClaudeTaskManager.get_task(task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        session_name = task.get('tmux_session', f'claude-{task_id}')
        if action == 'enter':
            cmd = ['tmux', 'copy-mode', '-t', session_name]
        else:
            cmd = ['tmux', 'send-keys', '-t', session_name, '-X', 'cancel']
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            self.send_json({
                'error': result.stderr.strip() or 'tmux command failed',
            }, 500)
            return
        self.send_json({'ok': True, 'mode': action})

    # ── Desktop launcher handlers ──────────────────────────────────────
    # All reads (GET /api/desktop, /api/desktop/{id}) pass through
    # check_claude_auth + allow_none_mode=True so the public-demo can
    # show the seeded launcher. Writes go through _readonly_block first so
    # the public-demo can't add/edit/delete icons.

    def handle_desktop_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'items': DesktopManager.list_items()})

    def handle_desktop_create(self):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            item = DesktopManager.create(body)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json(item, 201)

    def handle_desktop_update(self, item_id):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            item = DesktopManager.update(item_id, body)
        except ValueError as e:
            code = 404 if 'not found' in str(e) else 400
            self.send_json({'error': str(e)}, code)
            return
        self.send_json(item)

    def handle_desktop_delete(self, item_id):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            DesktopManager.delete(item_id)
        except ValueError as e:
            code = 404 if 'not found' in str(e) else 400
            self.send_json({'error': str(e)}, code)
            return
        self.send_json({'ok': True})

    def handle_desktop_reorder(self):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        ids = body.get('order') if isinstance(body, dict) else None
        try:
            items = DesktopManager.reorder(ids or [])
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'items': items})

    def handle_desktop_launch(self, item_id):
        """Execute the icon's action server-side. `task` returns the
        created task_id; `shell` returns stdout/stderr/exit_code; `url`
        rejects (client opens the URL directly, server is just bookkeeping).
        Mutations gated by _readonly_block — viewing the launcher in the
        public demo is fine, firing it isn't."""
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        item = DesktopManager.get(item_id)
        if not item:
            self.send_json({'error': 'item not found'}, 404)
            return
        action = item.get('action', {})
        kind = action.get('type')
        if kind == 'task':
            task = ClaudeTaskManager.create_task(
                action.get('prompt', ''),
                workdir=action.get('workdir') or '/home/dev',
                source=f'desktop:{item_id}',
                assistant=action.get('assistant'),
            )
            if task.get('status') == 'error':
                self.send_json({'error': task.get('error') or 'task spawn failed'}, 500)
                return
            self.send_json({'kind': 'task', 'task_id': task.get('task_id')}, 201)
        elif kind == 'shell':
            try:
                result = subprocess.run(
                    ['bash', '-lc', action.get('command', 'true')],
                    capture_output=True,
                    text=True,
                    timeout=int(action.get('timeout') or DesktopManager.SHELL_TIMEOUT_DEFAULT),
                    cwd='/home/dev',
                )
                self.send_json({
                    'kind': 'shell',
                    'exit_code': result.returncode,
                    'stdout': (result.stdout or '')[-8000:],
                    'stderr': (result.stderr or '')[-2000:],
                })
            except subprocess.TimeoutExpired:
                self.send_json({'error': 'command timed out', 'kind': 'shell'}, 504)
        elif kind == 'url':
            # The client opens URLs directly — no server work needed.
            # Return ok so the client can still report a launch event.
            self.send_json({'kind': 'url', 'url': action.get('url'), 'target': action.get('target', 'blank')})
        else:
            self.send_json({'error': f'unknown action type: {kind}'}, 400)

    def handle_webhook_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Public view — secrets are stripped out by WebhookManager._public_view
        self.send_json({'webhooks': WebhookManager.list_webhooks()})

    def handle_webhook_get(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = WebhookManager.get_webhook(self._webhook_id)
        if cfg is None:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        # Include the receive URL so the dashboard can render a copy button.
        cfg['receive_url'] = self._build_receive_url(self._webhook_id)
        self.send_json(cfg)

    def handle_webhook_create(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = WebhookManager.create_or_update(data)
        if err:
            self.send_json({'error': err}, 400)
            return
        # On create we surface the hmac_secret ONCE so the user can copy it
        # into the upstream service (GitHub/Stripe/etc.). After this, it's
        # only ever returned as hmac_secret_set: true.
        response = WebhookManager._public_view(cfg)
        if cfg.get('hmac_secret'):
            response['hmac_secret_once'] = cfg['hmac_secret']
        response['receive_url'] = self._build_receive_url(cfg['id'])
        self.send_json(response, 201)

    def handle_webhook_delete(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok = WebhookManager.delete(self._webhook_id)
        if not ok:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        self.send_json({'ok': True})

    def handle_webhook_receive(self):
        """Inbound receiver. Auth via HMAC of the raw body — NO bearer token.
        Triggers a Claude task and returns the task_id."""
        cfg = WebhookManager.get_webhook(self._webhook_id, include_secrets=True)
        if cfg is None:
            # Don't leak existence: same response as a real auth failure.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return

        # Read the raw body for HMAC verification BEFORE JSON parsing.
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length < 0 or content_length > 1 * 1024 * 1024:  # 1 MiB cap
            self.send_json({'error': 'payload too large'}, 413)
            return
        raw_body = self.rfile.read(content_length) if content_length else b''

        # Pass full headers — Slack/Stripe verifiers read multiple of them
        # (e.g. X-Slack-Request-Timestamp alongside X-Slack-Signature).
        if not WebhookManager.verify_signature(cfg, raw_body, self.headers):
            # Same shape as the not-found response to avoid leaking which is which.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return

        # Replay protection: reject identical signed bodies seen within the
        # 5-minute window. Provider-level timestamp checks (Slack/Stripe) and
        # this cache are belt-and-suspenders — Slack/Stripe alone allow up to
        # 5 minutes of replay; this cache closes that window to "exactly once".
        replay_key = (cfg['id'], hashlib.sha256(raw_body).hexdigest())
        if not WebhookManager.REPLAY_CACHE.check_and_record(replay_key):
            self.send_json({'error': 'duplicate request (replay)'}, 409)
            return

        try:
            payload = json.loads(raw_body.decode('utf-8')) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({'error': 'invalid JSON payload'}, 400)
            return

        self._fire_webhook(cfg, payload, status=202)

    def handle_webhook_test(self):
        """Dashboard 'Test' button: fire as if a real call came in, but with
        bearer auth instead of HMAC. Payload comes from the JSON body."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = WebhookManager.get_webhook(self._webhook_id, include_secrets=True)
        if cfg is None:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        payload = data.get('payload', {}) if isinstance(data, dict) else {}
        self._fire_webhook(cfg, payload, status=202)

    def _fire_webhook(self, cfg, payload, status=202):
        prompt = WebhookManager.render_prompt(cfg, payload)
        task = ClaudeTaskManager.create_task(
            prompt,
            workdir=cfg.get('workdir') or '/home/dev',
            response_url=cfg.get('response_url'),
            response_secret=cfg.get('response_secret'),
            source=f"webhook:{cfg['id']}",
        )
        # Propagate task-creation failures (tmux unreachable, fs error) so
        # the upstream sees a 5xx and can retry, rather than a 202 with a
        # task_id that never runs.
        if task.get('status') == 'error':
            self.send_json({
                'error': task.get('error') or 'failed to spawn task',
                'webhook_id': cfg['id'],
                'task_id': task.get('task_id'),
            }, 502)
            return
        self.send_json({
            'task_id': task['task_id'],
            'webhook_id': cfg['id'],
            'status': task['status'],
        }, status)

    def _build_receive_url(self, webhook_id):
        """Construct the public URL the upstream service should POST to.
        Uses the Host header; ingress strips /oauth so we don't prepend it."""
        host = self.headers.get('Host', '')
        proto = self.headers.get('X-Forwarded-Proto', 'https')
        if not host:
            return f'/api/webhooks/{webhook_id}'
        return f'{proto}://{host}/api/webhooks/{webhook_id}'

    # --- Cron handlers ---
    # CRUD uses check_claude_auth (dashboard / scripts). The cron-fire receiver
    # uses the per-cron fire_token instead — k8s CronJob pods are not part of
    # the OAuth session.

    def handle_cron_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        crons = CronManager.list_crons()
        # Decorate with k8s status — best-effort, won't fail the request.
        for c in crons:
            try:
                c.update(CronManager.kubectl_status(c['id']))
            except Exception:
                pass
        self.send_json({'crons': crons})

    def handle_cron_get(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = CronManager.get_cron(self._cron_id)
        if cfg is None:
            self.send_json({'error': 'Cron not found'}, 404)
            return
        cfg.update(CronManager.kubectl_status(self._cron_id))
        self.send_json(cfg)

    def handle_cron_create(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = CronManager.create_or_update(data)
        # err may be a "soft" error (k8s apply failed but config saved); still 4xx.
        if cfg is None:
            self.send_json({'error': err}, 400)
            return
        response = CronManager._public_view(cfg)
        if err:
            response['warning'] = err
            self.send_json(response, 202)
            return
        self.send_json(response, 201)

    def handle_cron_delete(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok = CronManager.delete(self._cron_id)
        if not ok:
            self.send_json({'error': 'Cron not found'}, 404)
            return
        self.send_json({'ok': True})

    def handle_cron_action(self):
        """suspend / resume / run — dashboard buttons."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        action = self._cron_action
        if action == 'suspend':
            cfg = CronManager.set_suspended(self._cron_id, True)
            if cfg is None:
                self.send_json({'error': 'Cron not found'}, 404)
                return
            self.send_json(CronManager._public_view(cfg))
        elif action == 'resume':
            cfg = CronManager.set_suspended(self._cron_id, False)
            if cfg is None:
                self.send_json({'error': 'Cron not found'}, 404)
                return
            self.send_json(CronManager._public_view(cfg))
        elif action == 'run':
            ok, info = CronManager.run_now(self._cron_id)
            if not ok:
                self.send_json({'error': info}, 500)
                return
            self.send_json({'ok': True, 'job': info})
        elif action == 'rotate-token':
            cfg, new_token = CronManager.rotate_token(self._cron_id)
            if cfg is None:
                self.send_json({'error': 'rotate failed (see pod logs)'}, 500)
                return
            response = CronManager._public_view(cfg)
            # One-time reveal of the new token, matching webhook secret-reveal UX
            response['fire_token_once'] = new_token
            self.send_json(response)
        else:
            self.send_json({'error': 'unknown action'}, 400)

    def handle_cron_fire(self):
        """Receiver called by the k8s CronJob pod with the per-cron fire_token.
        Renders the cron's prompt template against its static payload and
        spawns a Claude task. Never touches OAuth headers — this is an
        internal-cluster call."""
        auth = self.headers.get('Authorization', '')
        token = auth[7:].strip() if auth.startswith('Bearer ') else ''
        ok, cfg = CronManager.verify_fire_token(self._cron_id, token)
        if not ok or cfg is None:
            # Don't leak existence; same response for unknown id vs bad token.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return
        # Refuse to spawn tasks for suspended crons. Belt-and-suspenders: the
        # CronJob shouldn't fire when suspended, but if someone hits this
        # endpoint manually we want the suspend flag to be authoritative.
        if cfg.get('suspended'):
            self.send_json({'error': 'cron is suspended'}, 409)
            return
        prompt = CronManager.render_prompt(cfg)
        task = ClaudeTaskManager.create_task(
            prompt,
            workdir=cfg.get('workdir') or '/home/dev',
            response_url=cfg.get('response_url'),
            response_secret=cfg.get('response_secret'),
            source=f"cron:{cfg['id']}",
        )
        self.send_json({
            'task_id': task['task_id'],
            'cron_id': cfg['id'],
            'status': task['status'],
        }, 202)

    # --- Memory API handlers ---------------------------------------------
    # The dashboard's Memory tab consumes these endpoints. They mirror the
    # MCP tool surface (mcp_memory.py) so the dashboard and Claude share a
    # single SQLite store with consistent semantics.

    def _memory_unavailable(self):
        if _MEMORY_AVAILABLE:
            return False
        self.send_json({
            'error': 'memory subsystem unavailable',
            'detail': 'memory.manager failed to import; check server logs',
        }, 503)
        return True

    def _memory_actor(self):
        """Derive a stable `source` string for memory writes."""
        email = self.headers.get('X-Auth-Request-Email') or ''
        if email:
            return f'dashboard:{email}'
        user = self.headers.get('X-Auth-Request-User') or self.headers.get('Remote-User') or ''
        if user:
            return f'dashboard:{user}'
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            tok = auth_header[7:].strip()
            fp = hashlib.sha256(tok.encode()).hexdigest()[:8]
            return f'api:{fp}'
        return 'unknown'

    def _memory_error(self, e):
        if isinstance(e, MemNotFound):
            self.send_json({'error': str(e), 'code': 'not_found'}, 404)
            return
        if isinstance(e, MemConflict):
            self.send_json({'error': str(e), 'code': 'conflict'}, 409)
            return
        if isinstance(e, MemValidationError):
            self.send_json({'error': str(e), 'code': 'validation'}, 400)
            return
        if isinstance(e, MemError):
            self.send_json({'error': str(e), 'code': e.code}, 400)
            return
        print(f'[memory] internal error: {e}', file=sys.stderr)
        self.send_json({'error': str(e), 'code': 'internal'}, 500)

    def handle_memory_list(self, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = MemoryManager.list(
                namespace=(query.get('namespace') or [None])[0],
                kind=(query.get('kind') or [None])[0],
                q=(query.get('q') or [None])[0],
                limit=int((query.get('limit') or ['500'])[0]),
            )
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'memories': rows, 'count': len(rows)})

    def handle_memory_get(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            row = MemoryManager.get(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        if row is None:
            self.send_json({'error': 'not found', 'code': 'not_found'}, 404)
            return
        # Log read access.
        try:
            actor = self._memory_actor()
            kind = actor.split(':', 1)[0]
            ident = actor.split(':', 1)[1] if ':' in actor else actor
            MemoryManager.log_ref(
                namespace=namespace, key=key,
                ref_kind=kind if kind in ('dashboard', 'api', 'cron', 'task') else 'api',
                ref_id=ident, access_kind='read',
            )
        except Exception:
            pass
        self.send_json({'memory': row})

    def handle_memory_history(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = MemoryManager.history(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'revisions': rows, 'count': len(rows)})

    def handle_memory_refs(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = MemoryManager.refs(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'refs': rows, 'count': len(rows)})

    def handle_memory_neighbors(self, namespace, key, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        depth = int((query.get('depth') or ['1'])[0])
        kinds = query.get('kind') or query.get('kinds') or None
        try:
            rows = MemoryManager.neighbors(
                namespace=namespace, key=key, depth=depth,
                kinds=kinds,
            )
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'neighbors': rows, 'count': len(rows)})

    def handle_memory_stats(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            stats = MemoryManager.stats()
            # Surface syncer status so the dashboard can show "last
            # imported N from Claude's auto-memory · X minutes ago".
            try:
                stats['claude_sync'] = ClaudeMemorySyncer.status()
            except Exception:
                pass
            self.send_json(stats)
        except Exception as e:
            self._memory_error(e)

    def handle_memory_sync_claude(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            res = ClaudeMemorySyncer.trigger_sync()
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'status': 'ok', 'result': res})

    def handle_memory_upsert(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return
        try:
            row = MemoryManager.upsert(
                namespace=data.get('namespace', ''),
                key=data.get('key', ''),
                value=data.get('value', ''),
                kind=data.get('kind', 'semantic'),
                tags=data.get('tags', '') or '',
                importance=float(data.get('importance', 0.5)),
                confidence=float(data.get('confidence', 1.0)),
                source=self._memory_actor(),
                expires_at=data.get('expires_at'),
            )
        except Exception as e:
            self._memory_error(e); return
        try:
            actor = self._memory_actor()
            kind = actor.split(':', 1)[0]
            ident = actor.split(':', 1)[1] if ':' in actor else actor
            MemoryManager.log_ref(
                namespace=row['namespace'], key=row['key'],
                ref_kind=kind if kind in ('dashboard', 'api') else 'api',
                ref_id=ident, access_kind='write',
            )
        except Exception:
            pass
        self.send_json({'memory': row}, 200)

    def handle_memory_delete(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            row = MemoryManager.soft_delete(
                namespace=namespace, key=key,
                source=self._memory_actor(),
            )
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'memory': row, 'deleted': True})

    def handle_memory_link(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return
        try:
            rel = MemoryManager.link(
                src_namespace=namespace, src_key=key,
                dst_namespace=data.get('dst_namespace', ''),
                dst_key=data.get('dst_key', ''),
                kind=data.get('kind', 'related-to'),
                weight=float(data.get('weight', 1.0)),
                created_by=self._memory_actor(),
            )
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'relation': rel}, 201)

    def handle_memory_unlink(self, namespace, key, relation_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        # Direct SQL: the manager doesn't expose unlink yet because Phase 1
        # MCP tool surface doesn't need it. Dashboard only.
        try:
            with MemoryManager.store().tx() as c:
                cur = c.execute(
                    'DELETE FROM relations WHERE id=? AND src_id IN ('
                    '  SELECT id FROM memories WHERE namespace=? AND key=?'
                    ')',
                    (relation_id, namespace, key),
                )
                deleted = cur.rowcount
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'deleted': deleted}, 200 if deleted else 404)

    def handle_memory_consolidate(self):
        """Phase 1 stub. Returns 202 with a no-op so the dashboard button can
        be wired ahead of Phase 3 landing the real worker."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        self.send_json({
            'status': 'queued',
            'detail': 'consolidation worker activates in Phase 3',
        }, 202)

    # ── Subagents (spawned child tasks) ──────────────────────────
    # Lists real spawned sub-tasks filtered by parent_task_id.
    # Replaces the old read-only transcript scanner which was fragile
    # and version-dependent.

    def handle_subagents_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Parse ?parent=<task_id> filter from query string
        parent = None
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            parent_val = params.get('parent', [None])[0]
            if parent_val:
                parent = parent_val
        if not parent:
            self.send_json({'subagents': [], 'count': 0,
                            'running_count': 0, 'completed_count': 0,
                            'error_count': 0, 'note': 'pass ?parent=<task_id> to list sub-agents'})
            return
        tasks = ClaudeTaskManager.list_tasks(parent=parent)
        subagents = []
        running = 0
        completed = 0
        errored = 0
        for t in tasks:
            status = t['status']
            sa = {
                'tool_use_id': t['task_id'],
                'tool': 'spawn_agent',
                'timestamp': t['created_at'],
                'session_id': t['task_id'],
                'project': 'kube-coder',
                'description': t.get('prompt', '')[:200],
                'subagent_type': t.get('assistant', 'claude'),
                'prompt': t.get('prompt', ''),
                'status': status,
                'ended_at': t.get('finished_at'),
                'is_error': status == 'error',
            }
            subagents.append(sa)
            if status == 'running':
                running += 1
            elif status in ('completed',):
                completed += 1
            elif status in ('error', 'killed'):
                errored += 1
        self.send_json({
            'subagents': subagents,
            'count': len(subagents),
            'running_count': running,
            'completed_count': completed,
            'error_count': errored,
        })

    # ── Docs (in-app documentation site) ────────────────────────────────
    def handle_docs_manifest(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            manifest = DocsManager.load_manifest()
            self.send_json(manifest)
        except Exception as e:
            print(f'[docs] manifest error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)

    def handle_docs_page(self, page_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            page = DocsManager.get_page(page_id)
            self.send_json(page)
        except KeyError:
            self.send_json({'error': f'Unknown doc page: {page_id}'}, 404)
        except Exception as e:
            print(f'[docs] page {page_id} error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)

    def handle_docs_search(self, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        q = ''
        try:
            q = (query.get('q', [''])[0] or '').strip()
            try:
                limit = int(query.get('limit', ['25'])[0])
            except (ValueError, TypeError):
                limit = 25
            limit = max(1, min(100, limit))
            results = DocsManager.search(q, limit=limit)
            self.send_json({'q': q, 'results': results})
        except Exception as e:
            print(f'[docs] search {q!r} error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)

    # ── File upload + browse (rooted at /home/dev) ─────────────────────
    # We deliberately keep these endpoints scoped to /home/dev with a
    # realpath-based traversal check so a crafted X-Dest-Path can't escape
    # the user's home directory (e.g. via ".." or absolute paths).
    HOME_DEV = '/home/dev'
    MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB

    @classmethod
    def _resolve_under_home_dev(cls, rel_path: str) -> str:
        rel = (rel_path or '').strip()
        # Treat leading slashes as relative to /home/dev — users naturally
        # type "/screenshots" or "screenshots" interchangeably.
        rel = rel.lstrip('/')
        abs_path = os.path.realpath(os.path.join(cls.HOME_DEV, rel))
        if abs_path != cls.HOME_DEV and not abs_path.startswith(cls.HOME_DEV + os.sep):
            raise ValueError('path escapes /home/dev')
        return abs_path

    @staticmethod
    def _safe_filename(name: str) -> bool:
        if not name or name in ('.', '..'):
            return False
        if '/' in name or '\\' in name or '\x00' in name:
            return False
        if len(name) > 255:
            return False
        return True

    def handle_files_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        rel_dir = (params.get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel_dir)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isdir(target):
            self.send_json({'error': 'not a directory'}, 404)
            return
        entries = []
        try:
            for name in sorted(os.listdir(target), key=str.lower):
                if name.startswith('.'):  # hide dotfiles
                    continue
                full = os.path.join(target, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append({
                    'name': name,
                    'kind': 'dir' if os.path.isdir(full) else 'file',
                    'size': st.st_size,
                    'mtime': int(st.st_mtime),
                })
        except OSError as e:
            self.send_json({'error': f'list failed: {e}'}, 500)
            return
        # Surface dirs first so the UI can render a sensible tree.
        entries.sort(key=lambda e: (0 if e['kind'] == 'dir' else 1, e['name'].lower()))
        rel = os.path.relpath(target, self.HOME_DEV)
        if rel == '.':
            rel = ''
        self.send_json({'path': rel, 'entries': entries})

    def handle_file_upload(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Client URL-encodes both headers because HTTP header values are
        # ISO-8859-1; Unicode filenames (smart quotes, emoji, CJK, accented
        # letters) would otherwise trip fetch() in the browser. Decode here
        # before applying the safe-filename / under-home checks so the
        # validation runs on the actual intended path.
        rel_dir = urllib.parse.unquote((self.headers.get('X-Dest-Path') or '').strip())
        filename = urllib.parse.unquote((self.headers.get('X-Filename') or '').strip())
        if not self._safe_filename(filename):
            self.send_json({'error': 'invalid X-Filename header'}, 400)
            return
        try:
            dest_dir = self._resolve_under_home_dev(rel_dir)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            self.send_json({'error': 'invalid Content-Length'}, 400)
            return
        if content_length <= 0:
            self.send_json({'error': 'empty body'}, 400)
            return
        if content_length > self.MAX_UPLOAD_BYTES:
            self.send_json({'error': f'file too large (max {self.MAX_UPLOAD_BYTES} bytes)'}, 413)
            return
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            self.send_json({'error': f'mkdir failed: {e}'}, 500)
            return
        final_path = os.path.join(dest_dir, filename)
        # Stream to disk in 64 KiB chunks so a 200 MiB upload doesn't have to
        # fully buffer in memory before we touch the filesystem.
        try:
            with open(final_path, 'wb') as fh:
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            self.send_json({'error': f'write failed: {e}'}, 500)
            return
        try:
            size = os.path.getsize(final_path)
        except OSError:
            size = 0
        # Match perms to existing files in the target dir — workspace pods
        # run as a non-root user, so this is mostly a safety net.
        try:
            os.chmod(final_path, 0o644)
        except OSError:
            pass
        rel_out = os.path.relpath(final_path, self.HOME_DEV)
        self.send_json({
            'ok': True,
            'path': rel_out,
            'absolute_path': final_path,
            'size': size,
        }, 201)

    def handle_file_mkdir(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self.send_json({'error': 'invalid JSON body'}, 400)
            return
        rel_dir = (body.get('path') or '').strip()
        if not rel_dir:
            self.send_json({'error': 'path is required'}, 400)
            return
        try:
            target = self._resolve_under_home_dev(rel_dir)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            self.send_json({'error': f'mkdir failed: {e}'}, 500)
            return
        rel_out = os.path.relpath(target, self.HOME_DEV)
        if rel_out == '.':
            rel_out = ''
        self.send_json({'ok': True, 'path': rel_out}, 201)

    def send_vnc_viewer(self):
        # Defense-in-depth: oauth2-proxy should already have rejected an
        # unauth'd visitor, but if this handler is ever reached directly
        # (e.g. a misconfigured ingress) refuse rather than render the
        # iframe URL anyway. The deeper /vnc/<path> proxy IS authed; this
        # wrapper page used to slip through.
        if not self.check_claude_auth():
            self.send_response(401)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Unauthorized')
            return
        # Instead of embedding, redirect to the noVNC URL directly
        host = self.headers.get('Host', 'localhost').split(':')[0]
        vnc_url = f"https://{host}/vnc-direct/vnc.html?host={host}&port=6081&autoconnect=true&resize=scale"
        
        vnc_html = f'''<!DOCTYPE html>
<html>
<head>
    <title>VNC Viewer</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; text-align: center; }}
        .container {{ max-width: 600px; margin: 0 auto; }}
        .btn {{ background: #007cba; color: white; border: none; padding: 12px 24px; margin: 10px; border-radius: 4px; text-decoration: none; display: inline-block; }}
        .btn:hover {{ background: #005a8b; }}
        .warning {{ background: #fff3cd; border: 1px solid #ffeaa7; color: #856404; padding: 10px; border-radius: 4px; margin: 10px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🖥️ Remote Desktop Viewer</h1>
        <div class="warning">
            <strong>🔒 Secure Access:</strong> This VNC viewer is protected by authentication.
            You must be logged into this workspace to access the remote desktop.
        </div>
        <p>Click the button below to open the VNC viewer in a new window:</p>
        <a href="{vnc_url}" target="_blank" class="btn">Open VNC Viewer</a>
        <p><small>If the VNC viewer doesn't load, make sure you've launched a browser first.</small></p>
        <p><a href="/browser/">← Back to Browser Controls</a></p>
    </div>
</body>
</html>'''
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(vnc_html.encode())
    
    def redirect_to_vnc(self):
        # Defense-in-depth — see send_vnc_viewer above. This handler
        # actually proxies localhost:6081 content, so unauth'd access
        # would have exposed the VNC HTML directly.
        if not self.check_claude_auth():
            self.send_response(401)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Unauthorized')
            return
        # Redirect to the noVNC URL running on localhost:6081
        import urllib.request
        try:
            # Proxy the request to the local noVNC server
            vnc_url = "http://localhost:6081/vnc.html?autoconnect=true&resize=scale"
            with urllib.request.urlopen(vnc_url, timeout=10) as response:
                content = response.read()
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            # Escape so a crafted upstream error message can't inject HTML
            # into this authenticated origin (reflected XSS).
            error_html = f'''<!DOCTYPE html>
<html>
<head><title>VNC Connection Error</title></head>
<body>
    <h1>VNC Connection Error</h1>
    <p>Unable to connect to VNC server: {html.escape(str(e))}</p>
    <p><a href="/browser/">← Back to Browser Controls</a></p>
    <p>Make sure a browser is launched first, then try again.</p>
</body>
</html>'''
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(error_html.encode())

    # Per-CSP-directive splitter — used to strip frame-ancestors while keeping
    # the rest of the policy intact.
    _CSP_FRAME_ANCESTORS_RE = re.compile(r'(?:^|;)\s*frame-ancestors[^;]*', re.IGNORECASE)
    # Root-absolute src/href values in a proxied HTML body, e.g. src="/assets/x"
    # or href='/main.css'. The lookbehind skips data-src / srcset and similar
    # (no attr-boundary match); the value class stops at the closing quote,
    # whitespace or tag end. Bytes-mode so we never have to decode the body.
    _ABS_ASSET_URL_RE = re.compile(rb'(?<![\w-])((?:src|href)\s*=\s*)(["\'])(/[^"\'<>\s]*)')
    # Opening <head> tag — where we inject the runtime base-path shim.
    _HEAD_OPEN_RE = re.compile(rb'<head\b[^>]*>', re.IGNORECASE)
    # Injected into proxied HTML so an app's *runtime* requests (built in JS,
    # not in the HTML we rewrite) reach the right service through the proxy.
    # Runs in the browser, where the full client-visible prefix — including the
    # external /oauth auth segment that oauth2-proxy strips before requests
    # reach this server — IS visible via location.pathname. For fetch / XHR /
    # EventSource / WebSocket it rewrites:
    #   - root-absolute paths (`/api/x`)           → <prefix>/api/x  (this app's port)
    #   - same-origin absolute URLs                → same, via their path
    #   - localhost:<port> / 127.0.0.1:<port> URLs → /…/api-app-proxy/<port>/…
    #     so a separate backend ("API on :8086") the app talks to over loopback
    #     is reached through the proxy too — and becomes same-origin (no CORS).
    # Protocol-relative (//cdn), already-proxied, and port-less / external URLs
    # pass through. A classic inline <script> runs at parse time, before the
    # app's deferred module scripts, so the patches are in place first.
    _APP_PROXY_SHIM = (
        b'<script>(function(){'
        b'var p=location.pathname,k="/api/app-proxy/",ix=p.indexOf(k);if(ix<0)return;'
        b'var r=p.slice(ix+k.length),j=r.indexOf("/"),port=j<0?r:r.slice(0,j);'
        b'if(!port)return;'
        b'var P=p.slice(0,ix+k.length+port.length);'   # this app: /…/api-app-proxy/<port>
        b'var root=p.slice(0,ix+k.length-1);'          # proxy root: /…/api-app-proxy
        b'function pp(pt,pa){return root+"/"+pt+(pa||"/");}'
        b'function wsx(pt,pa){return (location.protocol==="https:"?"wss://":"ws://")+location.host+pp(pt,pa);}'
        b'var LH=/^https?:\\/\\/(?:localhost|127\\.0\\.0\\.1):(\\d+)(\\/[^\\s]*)?$/i;'
        b'var LW=/^wss?:\\/\\/(?:localhost|127\\.0\\.0\\.1):(\\d+)(\\/[^\\s]*)?$/i;'
        b'function fix(u){if(typeof u!=="string"||!u)return u;'
        b'var m=u.match(LH);if(m)return pp(m[1],m[2]);'                       # localhost:<port> → that port
        b'var o=location.origin+"/";if(u.indexOf(o)===0)u=u.slice(location.origin.length);'  # same-origin abs → path
        b'if(u.charAt(0)==="/"&&u.charAt(1)!=="/"&&u.indexOf(P+"/")!==0&&u.indexOf("/api/app-proxy/")!==0)return P+u;'
        b'return u;}'
        # fetch must be invoked with this===window; a bare call on a saved
        # reference throws "Illegal invocation", so bind it.
        b'var _f=window.fetch&&window.fetch.bind(window);if(_f){window.fetch=function(q,n){'
        b'if(typeof q==="string")return _f(fix(q),n);'
        b'if(q&&q.url){try{return _f(new Request(fix(q.url),q),n)}catch(e){}}'
        b'return _f(q,n)};}'
        b'var _x=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(){'
        b'if(arguments.length>1)arguments[1]=fix(arguments[1]);return _x.apply(this,arguments)};'
        b'if(window.EventSource){var E=window.EventSource;window.EventSource=function(u,c){return new E(fix(u),c)};'
        b'window.EventSource.prototype=E.prototype;}'
        b'if(window.WebSocket){var W=window.WebSocket;window.WebSocket=function(u,pr){'
        b'try{if(typeof u==="string"){var m=u.match(LW);'
        b'if(m)u=wsx(m[1],m[2]);'                                             # ws://localhost:<port> → that port
        b'else if(u.charAt(0)==="/"&&u.charAt(1)!=="/")u=wsx(port,u);}}catch(e){}'  # root-relative → this app
        b'return pr!==undefined?new W(u,pr):new W(u)};window.WebSocket.prototype=W.prototype;'
        # Preserve the readyState constants apps read as WebSocket.OPEN etc.
        b'window.WebSocket.CONNECTING=W.CONNECTING;window.WebSocket.OPEN=W.OPEN;'
        b'window.WebSocket.CLOSING=W.CLOSING;window.WebSocket.CLOSED=W.CLOSED;}'
        b'})();</script>'
    )
    # Hop-by-hop headers that must not be forwarded between client and
    # upstream. RFC 7230 §6.1 plus the usual extras.
    _HOP_BY_HOP_HEADERS = frozenset({
        'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
        'te', 'trailers', 'transfer-encoding', 'upgrade',
        'host', 'content-length',  # we re-derive these
    })

    def _dispatch_referer_proxy(self, method):
        """Recover a sub-resource that escaped the proxy prefix to the dashboard
        origin root — @font-face icon fonts (loaded by the CSS engine), lazy
        route chunks (dynamic import()), <img> srcs — i.e. requests the client
        shim can't rewrite. They arrive here as root-absolute paths and would
        404.

        When the Referer is one of our /api/app-proxy/<port>/ iframes (and the
        path isn't already a proxy path), 302-redirect it to the proxy path,
        reusing the Referer's own prefix — including the /oauth segment — so
        the redirect re-enters through oauth2-proxy and authenticates normally.

        We redirect rather than proxy inline on purpose: Referer is forgeable
        by non-browser clients, so proxying here would be an unauthenticated
        read path to loopback ports. The redirect target still enforces auth
        (a real browser carries the session cookie and follows the 3xx for
        fonts/images/modules; an unauthenticated client just gets bounced to
        login by oauth2-proxy).
        """
        ref = self.headers.get('Referer') or ''
        m = re.search(r'(/(?:oauth/|browser/)?api/app-proxy/(\d+))', ref)
        if not m:
            # TEMP diagnostic: an escaped navigation/sub-resource with no
            # usable app-proxy Referer would fall through to a 404. Log what
            # we got so we can see why (only for likely-escaped requests).
            dest = self.headers.get('Sec-Fetch-Dest', '')
            if ref or dest in ('document', 'iframe', 'empty'):
                try:
                    self.log_message('[app-escape] %s path=%s dest=%s mode=%s referer=%r',
                                     method, self.path, dest,
                                     self.headers.get('Sec-Fetch-Mode', ''), ref[:160])
                except Exception:
                    pass
            return False
        norm = self.path.split('?', 1)[0].replace('/oauth', '').replace('/browser', '')
        if norm.startswith('/api/app-proxy/'):
            return False  # already a proxy path — _dispatch_app_proxy handles it
        ok, _reason = AppsManager.is_proxyable(int(m.group(2)))
        if not ok:
            return False
        target = m.group(1) + (self.path if self.path.startswith('/') else '/' + self.path)
        self.send_response(302)
        self.send_header('Location', target)
        self.send_header('Content-Length', '0')
        self.end_headers()
        return True

    def _dispatch_app_proxy(self, claude_path, method):
        """Match /api/app-proxy/<port>/... and forward to the upstream.

        Centralizes the dispatch so every HTTP verb (GET/POST/PUT/DELETE/
        HEAD/OPTIONS) shares the same matching + auth + proxy code. The
        verb-specific do_* methods call this near the top of their /api
        routing chain; returns True if the request was handled.
        """
        m = re.match(r'^/api/app-proxy/(\d+)(/.*)?$', claude_path)
        if not m:
            return False
        port = int(m.group(1))
        upstream_path = m.group(2) or '/'
        # Preserve the original query string (stripped from claude_path
        # by the caller before normalization).
        qs = self.path.split('?', 1)
        if len(qs) == 2:
            upstream_path = upstream_path + '?' + qs[1]
        # GET requests carrying Upgrade: websocket are hijacked into a
        # raw bidirectional socket relay. Everything else is normal HTTP.
        if method == 'GET' and self.headers.get('Upgrade', '').lower() == 'websocket':
            self._proxy_app_websocket(port, upstream_path)
            return True
        self._proxy_app_request(port, upstream_path, method=method)
        return True

    def _proxy_app_websocket(self, port, upstream_path):
        """Hijack the underlying TCP socket and relay a WebSocket session
        between the client and the upstream app.

        BaseHTTPRequestHandler's request loop reads the next request line
        from `self.rfile` after `do_GET` returns. Setting
        `self.close_connection = True` stops that loop after we take over
        the socket. We never call `self.send_response()` ourselves — the
        upstream's 101 response is relayed verbatim so the client sees the
        real Sec-WebSocket-Accept handshake.
        """
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return
        ok, reason = AppsManager.is_proxyable(port)
        if not ok:
            self.send_response(403)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write((reason + '\n').encode())
            return

        # Compute the forwarded path the same way the HTTP proxy does.
        prefix = f'/api/app-proxy/{port}'
        pin = AppsManager.get_pin(port) or {}
        keep_prefix = bool(pin.get('strip_prefix', False))
        if not keep_prefix:
            forwarded_path = upstream_path or '/'
        else:
            forwarded_path = prefix + (upstream_path if upstream_path.startswith('/') else '/' + upstream_path)

        # Open the upstream socket.
        try:
            import socket as _socket
            upstream = _socket.create_connection(('127.0.0.1', port), timeout=5)
        except OSError as e:
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'WebSocket upstream unreachable: {e}\n'.encode())
            return

        # Send the upstream the original WebSocket handshake. Use the client's
        # headers minus Host/hop-by-hop; rewrite Origin so an app that whitelists
        # localhost still accepts the connection. Keep Sec-WebSocket-Key intact
        # so the upstream's Sec-WebSocket-Accept is valid for the client.
        try:
            req_lines = [f'GET {forwarded_path} HTTP/1.1']
            req_lines.append(f'Host: 127.0.0.1:{port}')
            for k, v in self.headers.items():
                kl = k.lower()
                if kl == 'host':
                    continue
                if kl == 'origin':
                    # Rewrite to the localhost form the upstream expects.
                    req_lines.append(f'Origin: http://localhost:{port}')
                    continue
                req_lines.append(f'{k}: {v}')
            req_lines.append('X-Forwarded-Prefix: ' + prefix)
            if self.headers.get('Host'):
                req_lines.append('X-Forwarded-Host: ' + self.headers['Host'])
            req_lines.append('')
            req_lines.append('')
            handshake = ('\r\n'.join(req_lines)).encode('iso-8859-1')
            upstream.sendall(handshake)
        except OSError as e:
            upstream.close()
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'WebSocket handshake failed: {e}\n'.encode())
            return

        # Read the upstream's response headers and stream the raw bytes
        # back to the client. We just need to slurp up to the blank line;
        # everything after that is opaque WebSocket frames.
        try:
            buf = bytearray()
            upstream.settimeout(10)
            while b'\r\n\r\n' not in buf:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            header_end = buf.find(b'\r\n\r\n')
            if header_end < 0:
                upstream.close()
                # Don't try to send any response — the framework hasn't seen
                # anything yet, so we can write a normal HTTP error.
                self.send_response(502)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'WebSocket upstream returned no response\n')
                return
            head = bytes(buf[:header_end + 4])
            leftover = bytes(buf[header_end + 4:])
        except OSError as e:
            upstream.close()
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'WebSocket upstream read failed: {e}\n'.encode())
            return

        # Take over the socket — don't let the framework write any more
        # headers or another request after we return.
        self.close_connection = True
        client_sock = self.connection
        try:
            # Relay the upstream's handshake response verbatim, then any
            # data that arrived in the same packet after the headers.
            client_sock.sendall(head)
            if leftover:
                client_sock.sendall(leftover)
        except OSError:
            upstream.close()
            return

        upstream.settimeout(None)
        client_sock.settimeout(None)

        # Manual access log so an admin can see the 101 in pod logs.
        try:
            self.log_message('"GET %s HTTP/1.1" 101 -', self.path)
        except Exception:
            pass

        # Bidirectional relay. One thread per direction; SHUT_WR on EOF
        # prevents a deadlock when one peer closes write but keeps reading.
        import socket as _socket

        def pipe(src, dst):
            try:
                while True:
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
            except (OSError, ConnectionResetError):
                pass
            finally:
                try:
                    dst.shutdown(_socket.SHUT_WR)
                except OSError:
                    pass

        t_up = threading.Thread(target=pipe, args=(client_sock, upstream), daemon=True)
        t_down = threading.Thread(target=pipe, args=(upstream, client_sock), daemon=True)
        t_up.start()
        t_down.start()
        t_up.join()
        t_down.join()
        try:
            upstream.close()
        except Exception:
            pass

    def _proxy_app_request(self, port, upstream_path, method='GET'):
        """Reverse-proxy a request to http://127.0.0.1:<port><upstream_path>.

        Streams the response body via read1+flush so SSE/chunked responses
        arrive promptly. Strips X-Frame-Options + CSP frame-ancestors from
        the upstream so the response can be embedded in the dashboard
        iframe. Rewrites absolute Location headers back to the proxy path.

        Path handling:
        - default (root-path-aware apps configured with --root-path /
          FORCE_SCRIPT_NAME / --base): strip the /api/app-proxy/<port>
          prefix before forwarding; the upstream's router expects the
          unprefixed path and uses X-Forwarded-Prefix for URL generation.
        - pinned port with strip_prefix=False: pass the full path through
          (the Vite-style case where the dev server only matches its own
          --base prefix).
        """
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return
        ok, reason = AppsManager.is_proxyable(port)
        if not ok:
            self.send_response(403)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write((reason + '\n').encode())
            return

        # Trailing-slash 301 so relative URLs resolve against the prefix root.
        if upstream_path in ('', '/'):
            normalized = self.path.split('?', 1)[0].replace('/oauth', '').replace('/browser', '')
            if normalized == f'/api/app-proxy/{port}':
                self.send_response(301)
                self.send_header('Location', f'/api/app-proxy/{port}/')
                self.end_headers()
                return

        # Apply the prefix-stripping rule based on the pin's flag.
        prefix = f'/api/app-proxy/{port}'
        pin = AppsManager.get_pin(port) or {}
        keep_prefix = bool(pin.get('strip_prefix', False))  # default: strip
        if not keep_prefix:
            # Strip the proxy prefix from the path we forward upstream.
            # The query string is already attached so just slice the path.
            if '?' in upstream_path:
                p, q = upstream_path.split('?', 1)
                forwarded_path = (p or '/') + '?' + q
            else:
                forwarded_path = upstream_path or '/'
        else:
            # Pass the full path through. The upstream is configured to
            # only match URLs starting with the proxy prefix.
            forwarded_path = prefix + (upstream_path if upstream_path.startswith('/') else '/' + upstream_path)

        # Read request body (if any).
        body = None
        body_len = int(self.headers.get('Content-Length') or 0)
        if body_len > 0:
            body = self.rfile.read(body_len)

        # Build forwarded headers. Drop hop-by-hop + ours; add X-Forwarded-*.
        fwd_headers = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in self._HOP_BY_HOP_HEADERS:
                continue
            # Ask the upstream for an identity-encoded body. We rewrite
            # absolute asset URLs in HTML responses (see below), which only
            # works on uncompressed bytes; over a localhost hop compression
            # buys nothing anyway.
            if kl == 'accept-encoding':
                continue
            # Rewrite Origin to the localhost form the upstream expects. Dev
            # servers (Metro, Vite, …) often 500/403 a request whose Origin is
            # a foreign host (anti-DNS-rebinding / CORS) — and @font-face fonts
            # and fetch()/XHR are CORS requests that carry Origin, so without
            # this icon fonts 500 and many API calls fail. Mirrors the
            # WebSocket proxy, which already rewrites Origin the same way.
            if kl == 'origin':
                v = f'http://localhost:{port}'
            fwd_headers[k] = v
        fwd_headers['Host'] = f'127.0.0.1:{port}'
        fwd_headers['X-Forwarded-Prefix'] = prefix
        fwd_headers['X-Forwarded-Proto'] = 'https' if self.headers.get('X-Forwarded-Proto') == 'https' else 'http'
        if 'Host' in self.headers:
            fwd_headers['X-Forwarded-Host'] = self.headers['Host']
        if body is not None:
            fwd_headers['Content-Length'] = str(len(body))

        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
            conn.request(method, forwarded_path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except (ConnectionRefusedError, OSError) as e:
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'Bad gateway: cannot reach 127.0.0.1:{port} ({e})\n'.encode())
            return

        # A 200 text/html body gets its root-absolute asset URLs made relative
        # so a stock build (Vite/CRA: <script src="/assets/x">) loads under the
        # proxy prefix instead of 404ing against the dashboard origin root (see
        # _rewrite_html_asset_urls). Buffered because the rewrite changes the
        # length; assets, JSON and SSE still stream untouched. The forwarded
        # request dropped Accept-Encoding, so this body is identity-encoded
        # and rewritable.
        ctype = resp.getheader('Content-Type', '') or ''
        rewrite_body = (
            method != 'HEAD'
            and resp.status == 200
            and 'text/html' in ctype.lower()
        )
        rewritten = self._rewrite_proxied_html(resp.read(), port) if rewrite_body else None

        # Forward status + filtered headers.
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            kl = k.lower()
            if kl in self._HOP_BY_HOP_HEADERS:
                continue
            if kl == 'x-frame-options':
                continue
            if kl == 'content-security-policy':
                stripped = self._CSP_FRAME_ANCESTORS_RE.sub('', v).strip().strip(';').strip()
                if not stripped:
                    continue
                v = stripped
            if kl == 'location':
                v = self._rewrite_location_header(v, port)
            self.send_header(k, v)
        if rewritten is not None:
            # Body length changed; the upstream's framing headers were already
            # dropped (hop-by-hop), so declare our own so the client doesn't
            # have to wait on connection close to know the body is complete.
            self.send_header('Content-Length', str(len(rewritten)))
        self.end_headers()

        if method == 'HEAD':
            pass
        elif rewritten is not None:
            try:
                self.wfile.write(rewritten)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            # Stream the body. read1 returns what's available so SSE heartbeats
            # arrive without waiting for a full read() to fill.
            try:
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, 'read1') else resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    try:
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            conn.close()
        except Exception:
            pass

    # --- Apps API (list + pin CRUD) ---

    def _handle_apps_list(self):
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return
        # Embedded iframes need cookie-based auth; bearer-only deployments
        # can't auth iframe sub-resource requests. Let the SPA show a clear
        # explanation instead of the user staring at mysterious 401s.
        unavailable = None
        if AUTH_MODE != 'oauth2':
            unavailable = ('Applications requires the workspace to run behind an OAuth2 '
                           'proxy so iframe sub-resource requests can authenticate via cookies. '
                           'Current AUTH_MODE is "{}".'.format(AUTH_MODE))
        try:
            apps = AppsManager.list_apps()
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
            return
        self.send_json({
            'apps': apps,
            'unavailable_reason': unavailable,
            'auth_mode': AUTH_MODE,
        })

    def _handle_apps_pin_create(self):
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return
        try:
            body = self.read_json_body(max_bytes=4096) or {}
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        except json.JSONDecodeError:
            self.send_json({'error': 'invalid JSON'}, 400)
            return
        try:
            pin = AppsManager.add_pin(
                port=body.get('port'),
                name=body.get('name'),
                strip_prefix=bool(body.get('strip_prefix', False)),
            )
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'ok': True, 'pin': {**pin, 'port': AppsManager._validate_port(body.get('port'))}}, 201)

    def _handle_apps_pin_delete(self, port):
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return
        try:
            removed = AppsManager.remove_pin(port)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'ok': True, 'removed': bool(removed)})

    # --- Additional HTTP verbs for the app proxy ---
    #
    # SimpleHTTPRequestHandler doesn't ship do_PUT / do_HEAD / do_OPTIONS, so
    # they 501 by default. The app proxy needs to forward all common verbs
    # so embedded apps (and their Try-it-out clients) work.

    def do_PUT(self):
        if self._readonly_block():
            return
        path = self._strip_route_prefix(self.path)
        if self._dispatch_app_proxy(path, 'PUT'):
            return
        self.send_response(501)
        self.end_headers()

    def do_PATCH(self):
        if self._readonly_block():
            return
        path = self._strip_route_prefix(self.path)
        if self._dispatch_app_proxy(path, 'PATCH'):
            return
        self.send_response(501)
        self.end_headers()

    def do_HEAD(self):
        path = self._strip_route_prefix(self.path)
        if self._dispatch_app_proxy(path, 'HEAD'):
            return
        if self._dispatch_referer_proxy('HEAD'):
            return
        # Fall back to the parent's static-file HEAD handling.
        return super().do_HEAD()

    def do_OPTIONS(self):
        path = self._strip_route_prefix(self.path)
        if self._dispatch_app_proxy(path, 'OPTIONS'):
            return
        # Permissive default — no CORS preflight wired for any other route.
        self.send_response(204)
        self.end_headers()

    def _rewrite_proxied_html(self, body, port):
        """Rewrite a proxied HTML body so a stock build renders AND can reach
        its backend through the /api/app-proxy/<port> prefix.

        Two parts:

        1. Static src/href URLs (parsed by the browser, not via JS) are made
           relative by dropping the leading slash. We relativize rather than
           prepend a prefix because the client reaches us through an external
           /oauth auth segment that oauth2-proxy strips before the request
           arrives here — so the server can neither see nor reconstruct the URL
           the browser actually used. A relative URL sidesteps that: the
           browser resolves it against the iframe document's real URL
           (…/oauth/api/app-proxy/<port>/ — trailing slash guaranteed by the
           301 above), keeping the /oauth prefix so it authenticates. Skips
           protocol-relative (//cdn) and already-proxied URLs; relative URLs
           are already correct.

        2. A runtime shim (_APP_PROXY_SHIM) is injected into <head> to catch
           requests the app builds in JS at request time — fetch('/runs'),
           XHR, EventSource, WebSocket — which relativizing the HTML can't
           touch. The shim re-prefixes those in the browser, where the full
           client-visible prefix is available.
        """
        def repl(mo):
            url = mo.group(3)
            if url.startswith(b'//') or url.startswith(b'/api/app-proxy/'):
                return mo.group(0)
            rel = url[1:]  # drop the single leading '/'
            return mo.group(1) + mo.group(2) + (rel or b'./')

        body = self._ABS_ASSET_URL_RE.sub(repl, body)

        # Inject (1) a permissive referrer policy and (2) the runtime shim,
        # right after <head> so they take effect before the app's own scripts.
        # The referrer meta makes the app's *navigations* carry the full iframe
        # URL as Referer — without it, a hard navigation (window.location) that
        # drops the app's base lands at the dashboard root with no Referer and
        # 404s, instead of being redirected back onto the proxy path by
        # _dispatch_referer_proxy.
        head_inject = b'<meta name="referrer" content="unsafe-url">' + self._APP_PROXY_SHIM
        if self._HEAD_OPEN_RE.search(body):
            body = self._HEAD_OPEN_RE.sub(
                lambda mo: mo.group(0) + head_inject, body, count=1)
        else:
            body = head_inject + body
        return body

    @staticmethod
    def _rewrite_location_header(value, port):
        """Map upstream-absolute Locations back to the proxy-prefixed path."""
        prefix = f'/api/app-proxy/{port}'
        for host_form in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            if value.startswith(host_form):
                return prefix + value[len(host_form):]
        # Absolute-path Location (e.g. "/foo") — re-prefix so the iframe
        # navigates through the proxy and not to the dashboard origin root.
        if value.startswith('/') and not value.startswith(prefix + '/') and value != prefix:
            return prefix + value
        return value

    def proxy_vnc_request(self):
        # Proxy requests to the local noVNC server.
        # Gate behind the same auth as the rest of the dashboard — the VNC
        # iframe is loaded from an already-authenticated SPA page, so callers
        # will always carry OAuth2 headers or a Bearer token.
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return

        import urllib.request
        import urllib.parse
        vnc_url = None
        try:
            # Split off path + query; reject anything with control characters
            # before we paste it into a URL. self.path is attacker-controllable.
            raw = self.path[5:]  # strip "/vnc/"
            if any(ord(c) < 0x20 or c in ('\x7f',) for c in raw):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'invalid characters in path')
                return
            if '?' in raw:
                path_part, query_part = raw.split('?', 1)
            else:
                path_part, query_part = raw, ''

            # Normalize and confine: drop empty / "." / ".." segments so the
            # caller cannot climb above /. The destination host is hardcoded
            # to localhost:6081, but a normalized path keeps proxied requests
            # to the shapes noVNC actually serves.
            segments = [
                seg for seg in path_part.split('/')
                if seg and seg not in ('.', '..')
            ]
            safe_path = '/'.join(
                urllib.parse.quote(urllib.parse.unquote(s), safe='') for s in segments
            )
            vnc_url = f"http://localhost:6081/{safe_path}"
            if query_part:
                vnc_url += f"?{query_part}"

            with urllib.request.urlopen(vnc_url, timeout=10) as response:
                content = response.read()
                content_type = response.headers.get('Content-Type', 'text/html')
                self.send_response(200)
                self.send_header('Content-type', content_type)
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            safe_url = html.escape(vnc_url) if vnc_url else 'N/A'
            error_html = f'''<!DOCTYPE html>
<html>
<head><title>VNC Proxy Error</title></head>
<body>
    <h1>VNC Proxy Error</h1>
    <p>Error accessing VNC: {html.escape(str(e))}</p>
    <p>Path: {html.escape(self.path)}</p>
    <p>VNC URL: {safe_url}</p>
</body>
</html>'''
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(error_html.encode())
    
    def do_POST(self):
        if self._readonly_block():
            return
        try:
            # Handle both /api/* and /browser/api/* and /oauth/browser/api/* paths
            path = self._strip_route_prefix(self.path)
            
            # /api/apps/pins — add a pinned port to the Applications page.
            if path == "/api/apps/pins":
                self._handle_apps_pin_create()
                return
            # /api/app-proxy/<port>/... — forward to a locally-listening
            # web app. Match early so it short-circuits the explicit
            # endpoint list below.
            if self._dispatch_app_proxy(path, 'POST'):
                return
            if path == "/api/launch-chrome":
                self.launch_chrome()
            elif path == "/api/open-localhost":
                self.open_localhost()
            elif path == "/api/test-chrome":
                self.test_chrome()
            # Keep Firefox endpoints for backward compatibility
            elif path == "/api/launch-firefox":
                self.launch_chrome()
            elif path == "/api/test-firefox":
                self.test_chrome()
            # GitHub configuration endpoints
            elif path == "/api/github/ssh/generate":
                self.handle_ssh_generate()
            elif path == "/api/github/config":
                self.handle_git_config_post()
            elif path == "/api/github/cli/login-url":
                self.handle_gh_login_instructions()
            elif path == "/api/github/cli/complete-auth":
                self.handle_gh_check_auth()
            # Claude Task API endpoints
            elif path == "/api/claude/tasks":
                self.handle_claude_create_task()
            elif path == "/api/claude/tasks/terminal":
                self.handle_claude_create_terminal_task()
            elif path == "/api/claude/auth/token/regenerate":
                self.handle_claude_regenerate_token()
            # Webhook CRUD (dashboard)
            elif path == "/api/webhooks":
                self.handle_webhook_create()
            # Cron CRUD (dashboard)
            elif path == "/api/crons":
                self.handle_cron_create()
            # Desktop launcher (dashboard)
            elif path == "/api/desktop":
                self.handle_desktop_create()
            elif path == "/api/desktop/_reorder":
                self.handle_desktop_reorder()
            # Memory API (dashboard surface; mirrored by MCP)
            elif path == "/api/memory":
                self.handle_memory_upsert()
            elif path == "/api/memory/_consolidate":
                self.handle_memory_consolidate()
            elif path == "/api/memory/_sync_claude":
                self.handle_memory_sync_claude()
            # File upload (raw body; X-Dest-Path + X-Filename headers)
            elif path == "/api/files/upload":
                self.handle_file_upload()
            # mkdir under /home/dev (JSON body: {path})
            elif path == "/api/files/mkdir":
                self.handle_file_mkdir()
            else:
                # /api/claude/tasks/{id}/message
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/message$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_followup()
                    return
                # /api/claude/tasks/{id}/rename
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/rename$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_rename_task()
                    return
                # /api/claude/tasks/{id}/prepare-terminal
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/prepare-terminal$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_prepare_terminal()
                    return
                # /api/claude/tasks/{id}/scroll-mode — toggle tmux copy-mode
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/scroll-mode$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_scroll_mode()
                    return
                # Desktop launcher per-item routes
                # PUT-like update (POST + id == "update"); /launch fires
                m = re.match(r'^/api/desktop/([a-z0-9]+)/launch$', path)
                if m:
                    self.handle_desktop_launch(m.group(1))
                    return
                m = re.match(r'^/api/desktop/([a-z0-9]+)$', path)
                if m:
                    self.handle_desktop_update(m.group(1))
                    return
                # /api/webhooks/{id}/test — fire as if from outside (dashboard)
                m = re.match(r'^/api/webhooks/([a-zA-Z0-9_-]+)/test$', path)
                if m:
                    self._webhook_id = m.group(1)
                    self.handle_webhook_test()
                    return
                # /api/webhooks/{id} — inbound receiver, HMAC-authed, NOT bearer.
                # Must come AFTER /test so /test is matched first.
                m = re.match(r'^/api/webhooks/([a-zA-Z0-9_-]+)$', path)
                if m:
                    self._webhook_id = m.group(1)
                    self.handle_webhook_receive()
                    return
                # Cron suspend/resume/run-now/rotate-token
                m = re.match(r'^/api/crons/([a-z0-9-]+)/(suspend|resume|run|rotate-token)$', path)
                if m:
                    self._cron_id = m.group(1)
                    self._cron_action = m.group(2)
                    self.handle_cron_action()
                    return
                # /api/triggers/cron-fire/{id} — receiver called by the
                # CronJob's curl pod. Bearer auth (fire_token), NOT OAuth.
                m = re.match(r'^/api/triggers/cron-fire/([a-z0-9-]+)$', path)
                if m:
                    self._cron_id = m.group(1)
                    self.handle_cron_fire()
                    return
                # Memory: relations endpoint takes a (ns, key) pair.
                m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/relations$', path)
                if m:
                    self.handle_memory_link(m.group(1), m.group(2))
                    return
                self.send_response(404)
                self.end_headers()
                self.wfile.write(f'API endpoint not found. Received: {self.path}'.encode())
        except Exception as e:
            self.send_error_response(f'Server error: {str(e)}')
    
    def send_success_response(self, message):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(message.encode())
    
    def send_error_response(self, message):
        self.send_response(500)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(message.encode())
    
    def send_livez(self):
        """Liveness probe — proves the HTTP server thread is alive and can
        answer, nothing more. Deliberately does ZERO blocking work: no socket
        connects to sub-services (see send_health_check), no auth, no disk, no
        JSON. On a 2-CPU pod a busy assistant task + many tmux-streaming
        handler threads can starve the GIL enough that a heavier handler can't
        finish inside the 10s liveness timeout for 3 straight probes (~90s),
        and the kubelet then SIGTERMs the container — killing the user's live
        tmux + tasks. A handler this cheap needs the GIL for only microseconds,
        so it returns even under heavy contention. Sub-service status belongs
        to /health (readiness) and the /health/* detail endpoints."""
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(b'ok')

    def send_health_check(self):
        """Overall health check endpoint - always returns 200 to avoid blocking"""
        vscode_status = self.check_service_health('localhost', 8080)
        terminal_status = self.check_service_health('localhost', 7681)
        browser_status = self.check_service_health('localhost', 6081)
        
        health_data = {
            'status': 'healthy' if (terminal_status and browser_status) else 'degraded',
            'services': {
                'vscode': {'status': 'up' if vscode_status else 'down', 'port': 8080},
                'terminal': {'status': 'up' if terminal_status else 'down', 'port': 7681},
                'browser': {'status': 'up' if browser_status else 'down', 'port': 6081}
            },
            'timestamp': time.time()
        }
        
        # Always return 200 to avoid blocking the service
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(health_data).encode())
    
    def send_vscode_health(self):
        """VS Code health check - always returns 200"""
        status = self.check_service_health('localhost', 8080)
        response = {'service': 'vscode', 'status': 'up' if status else 'down', 'port': 8080}
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())
    
    def send_terminal_health(self):
        """Terminal health check - always returns 200"""
        status = self.check_service_health('localhost', 7681)
        response = {'service': 'terminal', 'status': 'up' if status else 'down', 'port': 7681}
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())
    
    def send_browser_health(self):
        """Browser/VNC health check - always returns 200"""
        vnc_status = self.check_service_health('localhost', 5900)  # x11vnc
        websockify_status = self.check_service_health('localhost', 6081)  # websockify
        
        status = vnc_status and websockify_status
        response = {
            'service': 'browser',
            'status': 'up' if status else 'down',
            'components': {
                'vnc': 'up' if vnc_status else 'down',
                'websockify': 'up' if websockify_status else 'down'
            }
        }
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def send_metrics(self):
        """Send system metrics (CPU, memory, disk) as JSON.
        Auth-gated to avoid double-duty as an unauthenticated workload
        side-channel — public-demo callers get through via AUTH_MODE=none."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        metrics = MetricsCollector.get_all_metrics()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(metrics).encode())

    def send_github_status(self):
        """Send combined GitHub status as JSON.
        Strictly auth-gated (allow_none_mode=False) — the response leaks
        the SSH public-key fingerprint, gh CLI username, and git
        name/email, none of which should ever surface on a public demo."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        status = GitHubManager.get_full_status()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def send_git_config(self):
        """Send git config as JSON. Strictly auth-gated (allow_none_mode=False)
        — exposes the operator's git name + email."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        config = GitHubManager.get_git_config()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(config).encode())

    def handle_ssh_generate(self):
        """Handle SSH key generation request"""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}

            email = data.get('email', 'user@example.com')
            result = GitHubManager.generate_ssh_key(email)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def handle_git_config_post(self):
        """Handle git config update request"""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}

            name = data.get('name', '')
            email = data.get('email', '')

            if not name or not email:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Name and email are required'}).encode())
                return

            result = GitHubManager.set_git_config(name, email)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def handle_gh_login_instructions(self):
        """Return instructions for gh CLI authentication"""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        instructions = GitHubManager.start_device_flow()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(instructions).encode())

    def handle_gh_check_auth(self):
        """Check if gh CLI authentication is complete"""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        status = GitHubManager.get_gh_cli_status()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def check_service_health(self, host, port):
        """Check if a service is listening on the given port"""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                result = s.connect_ex((host, port))
                return result == 0
        except Exception:
            return False
    
    def test_chrome(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            # Test browser installation
            browser_paths = [
                '/usr/local/bin/browser',
                '/usr/bin/lynx',
                '/usr/bin/w3m', 
                '/usr/bin/firefox-esr',
                '/usr/bin/firefox',
                '/usr/bin/chromium-browser',
                '/usr/bin/google-chrome'
            ]
            
            browser_path = None
            for path in browser_paths:
                if os.path.exists(path):
                    browser_path = path
                    break
            
            if not browser_path:
                self.send_error_response('Browser not found. Installation may have failed.')
                return
            
            # Test Xvfb display
            display = os.environ.get('DISPLAY', ':99')
            try:
                result = subprocess.run(['xdpyinfo', '-display', display], 
                                       capture_output=True, text=True, timeout=5)
                if result.returncode != 0:
                    # xdpyinfo failed, but check if Xvfb process is running instead
                    xvfb_check = subprocess.run(['pgrep', 'Xvfb'], capture_output=True)
                    if xvfb_check.returncode != 0:
                        self.send_error_response(f'X11 display {display} not available')
                        return
            except (subprocess.TimeoutExpired, FileNotFoundError):
                # xdpyinfo not available or timed out, check if Xvfb process is running
                xvfb_check = subprocess.run(['pgrep', 'Xvfb'], capture_output=True)
                if xvfb_check.returncode != 0:
                    self.send_error_response(f'X11 display {display} not available (Xvfb not running)')
                    return
            
            self.send_success_response(f'✅ Browser found at: {browser_path}\n✅ X11 display {display} available')
            
        except Exception as e:
            self.send_error_response(f'Test failed: {str(e)}')
    
    def launch_chrome(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            # Try different Chrome/Chromium locations
            browser_commands = [
                ('/usr/local/bin/browser', []),
                ('/usr/bin/firefox-esr', ['--safe-mode']),
                ('/usr/bin/firefox', ['--safe-mode']),
                ('firefox-esr', ['--safe-mode']),
                ('firefox', ['--safe-mode']),
                ('chromium-browser', ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']),
                ('/usr/bin/chromium-browser', ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']),
                ('/usr/bin/google-chrome', ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'])
            ]
            
            browser_cmd = None
            browser_args = []
            for cmd, args in browser_commands:
                if os.path.exists(cmd) or subprocess.run(['which', cmd], capture_output=True).returncode == 0:
                    browser_cmd = cmd
                    browser_args = args
                    break
            
            if not browser_cmd:
                self.send_error_response('No Chrome browser found. Download may have failed.')
                return
            
            env = os.environ.copy()
            env['DISPLAY'] = ':99'
            
            # Launch browser in background
            cmd_list = [browser_cmd] + browser_args + ['--new-window']
            process = subprocess.Popen(
                cmd_list, 
                env=env,
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
            # Give it a moment to start
            time.sleep(2)
            
            if process.poll() is None:  # Process is still running
                self.send_success_response(f'✅ Chrome launched successfully (PID: {process.pid})')
            else:
                self.send_error_response('Chrome process exited immediately')
                
        except FileNotFoundError:
            self.send_error_response('Chrome not found. Please install Chrome first.')
        except Exception as e:
            self.send_error_response(f'Error launching Chrome: {str(e)}')
    
    def open_localhost(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            # Accept an optional {"port": <int>, "path": "<suffix>"} JSON
            # body so the dashboard's Preview pane can re-point the in-pod
            # browser without a code change. Path is appended after the
            # port (e.g. localhost:8080/admin or localhost:3000/?dev=1);
            # falls back to "/" when nothing is sent. The historical
            # port-only body still works.
            port = 8080
            url_path = '/'
            try:
                content_length = int(self.headers.get('Content-Length', 0) or 0)
                if content_length:
                    raw = self.rfile.read(content_length).decode('utf-8')
                    body = json.loads(raw) if raw else {}
                    if isinstance(body, dict):
                        if 'port' in body:
                            port = int(body['port'])
                        raw_path = str(body.get('path') or '').strip()
                        if raw_path:
                            # Normalize: ensure leading slash, no scheme,
                            # no host, no embedded newlines. Reject if it
                            # contains characters that don't belong in a
                            # path/query/fragment.
                            if '\n' in raw_path or '\r' in raw_path or ' ' in raw_path:
                                self.send_error_response('path must not contain whitespace or newlines')
                                return
                            if raw_path.lower().startswith(('http://', 'https://')):
                                self.send_error_response('path must be a relative suffix, not a full URL')
                                return
                            if not raw_path.startswith('/'):
                                raw_path = '/' + raw_path
                            url_path = raw_path
            except (ValueError, json.JSONDecodeError):
                self.send_error_response('Invalid JSON body — expected {"port": <int>, "path": "<suffix>"}')
                return
            if not (1 <= port <= 65535):
                self.send_error_response('port must be between 1 and 65535')
                return

            env = os.environ.copy()
            env['DISPLAY'] = ':99'

            url = f'http://localhost:{port}{url_path}'

            # Kill only browsers launched by this handler — pkill -f chrome
            # would also kill any user-spawned dev tool whose name includes
            # the substring (e.g. chrome-devtools-frontend). The marker dir
            # is unique to this handler and appears in every launched
            # browser's argv (chromium via --user-data-dir=, firefox via
            # -profile <dir>) so pkill -f on the literal path matches both.
            kc_user_data_dir = '/tmp/kc-managed-browser'
            os.makedirs(kc_user_data_dir, exist_ok=True)
            subprocess.run(['pkill', '-f', kc_user_data_dir],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            time.sleep(0.3)

            # --app and --start-fullscreen together give a kiosk-like surface:
            # no tabs, no URL bar, no window chrome — just the page. Combined
            # with vnc.html?resize=scale on the dashboard side, the Preview
            # pane ends up showing essentially only the browser content.
            # --user-data-dir is the marker pkill uses above to scope the
            # kill to only browsers we launched.
            chrome_args = [
                '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                f'--user-data-dir={kc_user_data_dir}',
                '--start-fullscreen', f'--app={url}',
            ]
            # Firefox uses -profile <dir>; we mirror the chromium marker so
            # both can be killed by the single pkill above.
            firefox_args = ['--safe-mode', '-profile', kc_user_data_dir, '--kiosk', url]

            browser_commands = [
                ('chromium-browser', chrome_args),
                ('/usr/bin/chromium-browser', chrome_args),
                ('/usr/bin/google-chrome', chrome_args),
                ('/usr/local/bin/browser', []),
                ('/usr/bin/firefox-esr', firefox_args),
                ('/usr/bin/firefox', firefox_args),
                ('firefox-esr', firefox_args),
                ('firefox', firefox_args),
            ]

            browser_cmd = None
            browser_args = []
            for cmd, args in browser_commands:
                if os.path.exists(cmd) or subprocess.run(['which', cmd], capture_output=True).returncode == 0:
                    browser_cmd = cmd
                    browser_args = args
                    break

            if not browser_cmd:
                self.send_error_response('No Chrome browser found. Download may have failed.')
                return

            # If we fell through to /usr/local/bin/browser (no args defined),
            # append the URL so it still navigates somewhere.
            cmd_list = [browser_cmd] + browser_args
            if browser_cmd == '/usr/local/bin/browser':
                cmd_list.append(url)

            process = subprocess.Popen(
                cmd_list,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Give it a moment to start
            time.sleep(1)

            if process.poll() is None:  # Process is still running
                self.send_success_response(f'✅ Chrome opened with {url} (PID: {process.pid})')
            else:
                self.send_error_response('Chrome process exited immediately')

        except FileNotFoundError:
            self.send_error_response('Chrome not found. Please install Chrome first.')
        except Exception as e:
            self.send_error_response(f'Error opening localhost in Chrome: {str(e)}')

if __name__ == "__main__":
    # Change to the directory containing our files
    os.chdir('/tmp/browser')

    # Initialize the persistent-memory subsystem (runs migrations, opens DB).
    # Failure here is non-fatal: the rest of the server keeps working, and
    # /api/memory* returns 503 until the import error is fixed.
    if _MEMORY_AVAILABLE:
        try:
            MemoryManager.store()
            print(f'[memory] initialized at /home/dev/.claude-memory/memory.db')
        except Exception as e:
            print(f'[memory] init failed: {e}', file=sys.stderr)
        # Start background sync of Claude Code's native auto-memory files
        # (~/.claude/projects/*/memory/*.md) into the SQLite store so they
        # appear in the dashboard alongside dashboard- and MCP-authored
        # entries. One-way, idempotent, skips unchanged via mtime tag.
        try:
            ClaudeMemorySyncer.start(interval_seconds=60)
            print('[memory] claude-auto-memory syncer started (60s)')
        except Exception as e:
            print(f'[memory] syncer start failed: {e}', file=sys.stderr)

    print("Starting Browser API Server on port 6080...")
    print("Available endpoints:")
    print("  GET  /           - Browser interface")
    print("  POST /api/launch-chrome - Launch Chrome")
    print("  POST /api/open-localhost - Open localhost:8080 in Chrome")
    print("  POST /api/test-chrome   - Test Chrome installation")
    print("  POST /api/launch-firefox - Launch Chrome (legacy endpoint)")
    print("  POST /api/test-firefox   - Test Chrome (legacy endpoint)")
    print("  --- Claude Task API ---")
    print("  POST /api/claude/tasks              - Create new task")
    print("  POST /api/claude/tasks/terminal     - Create plain-bash terminal task")
    print("  GET  /api/claude/tasks              - List all tasks")
    print("  GET  /api/claude/tasks/{id}         - Get task detail + output")
    print("  GET  /api/claude/tasks/{id}/output  - Get raw output")
    print("  POST /api/claude/tasks/{id}/message - Send follow-up prompt")
    print("  POST /api/claude/tasks/{id}/rename  - Rename a task")
    print("  DELETE /api/claude/tasks/{id}       - Kill a running task")
    print("  GET  /api/claude/auth/token         - Get bearer token (OAuth2 only)")
    print("  POST /api/claude/auth/token/regenerate - Regenerate token (OAuth2 only)")
    print("  GET  /api/claude/assistants         - List enabled assistants")
    print("  --- Memory API (Phase 1) ---")
    print("  GET    /api/memory                       - List/search memories")
    print("  POST   /api/memory                       - Upsert a memory")
    print("  GET    /api/memory/{ns}/{key}            - Get one memory")
    print("  DELETE /api/memory/{ns}/{key}            - Soft-delete a memory")
    print("  GET    /api/memory/{ns}/{key}/history    - Revisions")
    print("  GET    /api/memory/{ns}/{key}/refs       - Access log")
    print("  GET    /api/memory/{ns}/{key}/neighbors  - Graph walk")
    print("  POST   /api/memory/{ns}/{key}/relations  - Create relation")
    print("  GET    /api/memory/stats                 - Counts + health")

    with http.server.ThreadingHTTPServer(("", 6080), BrowserHandler) as httpd:
        httpd.serve_forever()