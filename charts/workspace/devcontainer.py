"""devcontainer.json reader for kube-coder workspaces (#594).

WHY THIS EXISTS. `devcontainer.json` is the file a repo already carries to
describe its own dev environment — runtimes, setup commands, forwarded ports,
editor extensions. Codespaces, Coder, Ona and DevPod all read it. kube-coder
bakes its environment into one static image, so a team arriving from any of
those has to rebuild their setup by hand. That is a migration tax, not a
capability gap: reading the file they already have turns "rebuild everything"
into "point it at the repo".

SCOPE — we interpret the file inside the existing pod. We do not build a
container, and we never will from here. Three verified constraints force it:

  * The pod runs UID 1000 with allowPrivilegeEscalation: false
    (templates/deployment.yaml), so `sudo` does not work at runtime. The spec
    requires `features` to install AS ROOT AT IMAGE BUILD TIME — structurally
    impossible here, not merely unimplemented.
  * build.mode defaults to kaniko (values.yaml), so there is no Docker daemon.
  * One pod is one persistent home; a nested dev container would mean two
    filesystems and code-server/tmux/agents would have to pick one.

So a large part of this module is about reporting what CANNOT be applied, with
a reason grounded in those constraints and a remedy. Silently ignoring
`features` would produce a workspace that looks configured and isn't — the
worst possible failure mode for a migration feature.

DESIGN — pure, like instruction_scan.py:

  * Nothing here executes anything. `subprocess` is deliberately not imported.
    The impure half (running lifecycle commands, pinning ports, writing
    code-server settings) lives in server.py's DevcontainerManager, which can
    reach AppsManager/EventBroker without this module importing `server` and
    creating a cycle. Same split as skills: pure model + impure syncer.
  * Deterministic, no LLM, no network.
  * The three denylists (env, settings, extension ids) are the highest-value
    code in the file. A repo-controlled `ANTHROPIC_BASE_URL` would silently
    proxy every agent prompt through an attacker's endpoint; a repo-controlled
    `terminal.integrated.profiles` runs a command every time the user opens a
    terminal, forever. They matter even in a build that never executes a
    lifecycle command.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    'DevcontainerError',
    'CONFIG_CANDIDATES', 'HOOKS', 'HOOK_KEYS', 'MAX_BYTES',
    'loads_jsonc', 'strip_comments', 'strip_trailing_commas',
    'find_config', 'parse', 'summarize',
    'content_hash', 'hook_hash',
    'normalize_commands', 'normalize_ports', 'normalize_extensions',
    'normalize_settings', 'normalize_env', 'map_workspace_folder',
    'classify_unsupported', 'substitute_vars',
    'read_state', 'write_state', 'get_record', 'put_record', 'drop_record',
    'boot_id', 'lifecycle_status', 'log_path_for',
]


class DevcontainerError(Exception):
    """Any failure to locate, read or parse a devcontainer.json.

    Carries `line`/`column` when the underlying failure was a syntax error, so
    the UI can say "invalid at line 12, column 3" against the ORIGINAL file.
    """

    def __init__(self, message: str, line: int = 0, column: int = 0):
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column


# ── locating the file ────────────────────────────────────────────────────────

# Precedence per the spec: the canonical folder form first, then the
# repo-root dotfile. A `.devcontainer/<name>/devcontainer.json` (the
# multi-configuration layout) is picked up last, lowest name first so the
# choice is deterministic rather than filesystem-order dependent.
CONFIG_CANDIDATES = ('.devcontainer/devcontainer.json', '.devcontainer.json')

# Refuse anything larger before reading it. Real configs are a few KiB; a
# 50 MiB "devcontainer.json" is an attempt to wedge the parser, not a config.
MAX_BYTES = 256 * 1024

# Lifecycle hooks we support, in spec execution order. `initializeCommand`
# (runs on the CLIENT, before the container exists) and `postAttachCommand`
# (runs per editor attach, which has no analogue for a long-lived pod) are
# deliberately absent — see UNSUPPORTED_KEYS.
HOOKS = ('onCreate', 'updateContent', 'postCreate', 'postStart')
HOOK_KEYS = {
    'onCreate': 'onCreateCommand',
    'updateContent': 'updateContentCommand',
    'postCreate': 'postCreateCommand',
    'postStart': 'postStartCommand',
}


def find_config(workdir: str) -> str:
    """Absolute path of the devcontainer.json under `workdir`, '' if none."""
    for rel in CONFIG_CANDIDATES:
        path = os.path.join(workdir, *rel.split('/'))
        if os.path.isfile(path):
            return path
    # .devcontainer/<folder>/devcontainer.json — deterministic pick.
    holder = os.path.join(workdir, '.devcontainer')
    try:
        names = sorted(os.listdir(holder))
    except OSError:
        return ''
    for name in names:
        path = os.path.join(holder, name, 'devcontainer.json')
        if os.path.isfile(path):
            return path
    return ''


def read_raw(path: str) -> str:
    """File text, size-capped. Raises DevcontainerError, never OSError."""
    try:
        size = os.stat(path).st_size
    except OSError as e:
        raise DevcontainerError(f'cannot stat {path}: {e}')
    if size > MAX_BYTES:
        raise DevcontainerError(
            f'devcontainer.json is {size} bytes; the limit is {MAX_BYTES}')
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='strict') as f:
            return f.read()
    except UnicodeDecodeError as e:
        raise DevcontainerError(f'devcontainer.json is not valid UTF-8: {e}')
    except OSError as e:
        raise DevcontainerError(f'cannot read {path}: {e}')


# ── JSONC ────────────────────────────────────────────────────────────────────
#
# devcontainer.json is JSON *with comments and trailing commas* — both are
# legal and both appear in nearly every real-world file, so `json.loads` alone
# fails on the common case. Two string-aware passes, then stdlib json. No
# dependency: the workspace image ships no third-party JSONC parser and adding
# one for this would be a supply-chain edge we don't need.
#
# Both passes preserve LENGTH and LINE COUNT: a comment character becomes a
# space, a newline inside a block comment stays a newline. So a JSONDecodeError
# from the third step reports a line/column that points into the ORIGINAL file.
# That is what makes the error message honest instead of off-by-N.


def strip_comments(text: str) -> str:
    """Blank out // and /* */ comments, preserving offsets and line count."""
    out: List[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == '\\':
                # Escape: copy the escaped char verbatim so a \" does not end
                # the string and a trailing backslash cannot run off the end.
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                out.append(' ')
                i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            # Block comments do NOT nest in JSONC: the first */ closes.
            end = text.find('*/', i + 2)
            if end == -1:
                line = text.count('\n', 0, i) + 1
                raise DevcontainerError(
                    'unterminated block comment', line=line,
                    column=i - (text.rfind('\n', 0, i) + 1) + 1)
            for k in range(i, end + 2):
                out.append('\n' if text[k] == '\n' else ' ')
            i = end + 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def strip_trailing_commas(text: str) -> str:
    """Blank out a comma whose next non-space character closes the container.

    Runs AFTER strip_comments, so `[1, 2, // done\\n]` needs no special case:
    the comment is already spaces and the lookahead skips it for free.
    """
    chars = list(text)
    i, n = 0, len(chars)
    in_string = False
    while i < n:
        ch = chars[i]
        if in_string:
            if ch == '\\':
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == ',':
            j = i + 1
            while j < n and chars[j] in ' \t\r\n':
                j += 1
            if j < n and chars[j] in '}]':
                chars[i] = ' '
        i += 1
    return ''.join(chars)


def loads_jsonc(text: str) -> Dict[str, Any]:
    """Parse JSON-with-comments into a dict. Raises DevcontainerError."""
    if isinstance(text, bytes):          # tolerate raw bytes from a caller
        try:
            text = text.decode('utf-8-sig')
        except UnicodeDecodeError as e:
            raise DevcontainerError(f'not valid UTF-8: {e}')
    if text.startswith('﻿'):
        # Replace rather than slice: a BOM is one character, and swapping it
        # for a space keeps every later offset (and the error line/col) exact.
        text = ' ' + text[1:]
    cleaned = strip_trailing_commas(strip_comments(text))
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise DevcontainerError(
            f'invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}',
            line=e.lineno, column=e.colno)
    if not isinstance(data, dict):
        raise DevcontainerError(
            'devcontainer.json must contain a JSON object at the top level')
    return data


# ── hashing ──────────────────────────────────────────────────────────────────


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, default=str)


def content_hash(raw: str) -> str:
    """Hash of the file text. Identity for the consent compare-and-swap."""
    return hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()


def hook_hash(commands: List[Dict[str, Any]]) -> str:
    """Hash of ONE hook's normalized command list.

    Per hook, not per file, on purpose: a whole-file hash means editing
    `forwardPorts` marks a successful `postCreate` stale and re-prompts the
    user to re-run a ten-minute install that has not changed.
    """
    shape = [[c.get('name', ''), c.get('kind'), c.get('command')]
             for c in (commands or [])]
    return hashlib.sha256(_canonical(shape).encode('utf-8')).hexdigest()


# ── variable substitution ────────────────────────────────────────────────────

_VAR_RE = re.compile(r'\$\{([A-Za-z0-9_:.\-]+)\}')

# ${localEnv:X} is DELIBERATELY not resolved. It would copy a workspace secret
# (ANTHROPIC_API_KEY, GH_TOKEN, …) into a value the repo chose the name of, and
# a postStartCommand from that same repo can read it back and POST it anywhere.
# Reported as an unresolved caveat instead, with the literal token shown.
_UNRESOLVABLE_PREFIXES = ('localEnv:', 'containerEnv:', 'devcontainerId')


def substitute_vars(value: str, workdir: str) -> Tuple[str, List[str]]:
    """Expand the workspace-folder variables. Returns (text, unresolved)."""
    base = os.path.basename(os.path.normpath(workdir))
    table = {
        'workspaceFolder': workdir,
        'containerWorkspaceFolder': workdir,
        'localWorkspaceFolder': workdir,
        'workspaceFolderBasename': base,
        'containerWorkspaceFolderBasename': base,
        'localWorkspaceFolderBasename': base,
    }
    unresolved: List[str] = []

    def _sub(m):
        name = m.group(1)
        if name in table:
            return table[name]
        unresolved.append(m.group(0))
        return m.group(0)

    return _VAR_RE.sub(_sub, value), unresolved


# ── lifecycle commands ───────────────────────────────────────────────────────

# Tokens that mean "this needs root". The pod cannot escalate, so a command
# containing one WILL fail — surfacing that before the user consents turns a
# confusing 15-minute failure into an upfront, actionable warning.
_ROOT_TOKENS = frozenset({
    'sudo', 'su', 'apt', 'apt-get', 'aptitude', 'yum', 'dnf', 'apk', 'dpkg',
    'rpm', 'zypper', 'docker', 'dockerd', 'systemctl', 'service', 'usermod',
    'useradd', 'groupadd', 'chown', 'chgrp', 'update-alternatives',
    'ldconfig', 'mount', 'setcap',
})
_WORD_RE = re.compile(r'[A-Za-z0-9_.\-/]+')


def _needs_root(text: str) -> List[str]:
    reasons = []
    words = _WORD_RE.findall(text or '')
    hits = sorted({w for w in words if w in _ROOT_TOKENS})
    for h in hits:
        reasons.append(f'`{h}` requires root')
    for p in ('/usr/', '/etc/', '/opt/', '/var/lib/'):
        if p in (text or '') and re.search(
                r'(>|>>|tee|cp|mv|mkdir|install|ln)\s[^\n]*' + re.escape(p),
                text or ''):
            reasons.append(f'writes under {p}')
            break
    return reasons


def _one_command(name: str, value: Any, workdir: str) -> Optional[Dict[str, Any]]:
    """Normalize a single command value (string or argv list)."""
    caveats: List[str] = []
    if isinstance(value, str):
        text, unresolved = substitute_vars(value, workdir)
        if not text.strip():
            return None
        entry = {'name': name, 'kind': 'shell', 'command': text,
                 'display': text}
    elif isinstance(value, list):
        parts = []
        unresolved: List[str] = []
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise DevcontainerError(
                    f'{name or "command"}: array entries must be strings')
            sub, u = substitute_vars(str(item), workdir)
            unresolved.extend(u)
            parts.append(sub)
        if not parts:
            return None
        entry = {'name': name, 'kind': 'argv', 'command': parts,
                 'display': ' '.join(parts)}
    else:
        raise DevcontainerError(
            f'{name or "command"}: expected a string, array or object')
    if unresolved:
        caveats.append('unresolved variables: ' + ', '.join(sorted(set(unresolved))))
    root = _needs_root(entry['display'])
    entry['needs_root'] = bool(root)
    entry['root_reasons'] = root
    entry['caveats'] = caveats
    return entry


def normalize_commands(value: Any, workdir: str) -> List[Dict[str, Any]]:
    """A hook value -> an ordered list of command entries.

    Three spec forms: string (run through a shell), array (exec'd directly,
    never concatenated into a shell string), object (named commands). The spec
    says object entries run in PARALLEL; we run them sequentially in key order.
    Parallelism buys nothing for a handful of setup commands and it ruins both
    failure attribution and log readability — reported as a caveat.
    """
    if value is None:
        return []
    if isinstance(value, dict):
        out = []
        for key in value.keys():
            entry = _one_command(str(key), value[key], workdir)
            if entry is not None:
                entry['caveats'] = list(entry['caveats']) + [
                    'runs sequentially; the spec allows parallel']
                out.append(entry)
        return out
    entry = _one_command('', value, workdir)
    return [entry] if entry is not None else []


# ── ports ────────────────────────────────────────────────────────────────────

# Mirrors AppsManager.INTERNAL_PORTS. Duplicated rather than imported because
# this module must not import server; DevcontainerManager asserts they agree.
INTERNAL_PORTS = frozenset({22, 2376, 5900, 6080, 6081, 7681, 8080})


def normalize_ports(forward_ports: Any,
                    ports_attributes: Any) -> Tuple[List[Dict[str, Any]],
                                                    List[Dict[str, Any]]]:
    """(accepted, skipped). Accepted entries carry the portsAttributes label."""
    accepted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    attrs = ports_attributes if isinstance(ports_attributes, dict) else {}

    def _label(port: int) -> str:
        a = attrs.get(str(port)) or attrs.get(port)
        if isinstance(a, dict):
            lbl = a.get('label')
            if isinstance(lbl, str) and lbl.strip():
                return lbl.strip()[:80]
        return f'port {port}'

    seen = set()
    for raw in (forward_ports or []):
        if isinstance(raw, bool):        # bool is an int subclass — reject first
            skipped.append({'value': raw, 'reason': 'not a port number'})
            continue
        if isinstance(raw, int):
            port = raw
        elif isinstance(raw, str) and raw.isdigit():
            port = int(raw)
        elif isinstance(raw, str) and ':' in raw:
            # "db:5432" forwards a port on ANOTHER compose service. There is no
            # other service — this workspace is a single pod.
            skipped.append({
                'value': raw,
                'reason': 'host:port forwarding targets another container; '
                          'this workspace is a single pod'})
            continue
        else:
            skipped.append({'value': raw, 'reason': 'not a port number'})
            continue
        if not (1 <= port <= 65535):
            skipped.append({'value': raw, 'reason': 'port out of range'})
            continue
        if port in INTERNAL_PORTS:
            skipped.append({'value': port,
                            'reason': f'port {port} is reserved by the workspace'})
            continue
        if port in seen:
            continue
        seen.add(port)
        accepted.append({'port': port, 'label': _label(port)})
    return accepted, skipped


# ── extensions ───────────────────────────────────────────────────────────────

# publisher.name, optionally @version. Strict on purpose: code-server's
# --install-extension also accepts a PATH, so a value like
# "./evil.vsix" or "../../tmp/x.vsix" would install arbitrary editor code
# straight out of the cloned repo, and a leading "-" would be read as a flag.
_EXT_RE = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.[A-Za-z0-9][A-Za-z0-9_.-]{0,63}'
    r'(@[0-9][A-Za-z0-9.\-]{0,31})?$')


def normalize_extensions(value: Any) -> Tuple[List[str], List[Dict[str, str]]]:
    accepted: List[str] = []
    rejected: List[Dict[str, str]] = []
    seen = set()
    for raw in (value or []):
        if not isinstance(raw, str):
            rejected.append({'value': str(raw), 'reason': 'not a string'})
            continue
        ext = raw.strip()
        low = ext.lower()
        if not ext:
            continue
        if low.endswith('.vsix') or '/' in ext or '\\' in ext:
            rejected.append({
                'value': ext,
                'reason': 'looks like a file path — only marketplace ids '
                          '(publisher.name) are installed'})
            continue
        if not _EXT_RE.match(ext):
            rejected.append({
                'value': ext,
                'reason': 'not a valid extension id (publisher.name)'})
            continue
        key = low.split('@', 1)[0]
        if key in seen:
            continue
        seen.add(key)
        accepted.append(ext)
    return accepted, rejected


# ── settings ─────────────────────────────────────────────────────────────────

# code-server settings that execute code or redirect traffic. A repo that sets
# terminal.integrated.profiles runs its command every time the user opens a
# terminal — silently, forever, long after the repo is forgotten.
_SETTING_DENY_PREFIXES = (
    'terminal.integrated.defaultprofile',
    'terminal.integrated.profiles',
    'terminal.integrated.automationprofile',
    'terminal.integrated.automationshell',
    'terminal.integrated.shell',
    'terminal.integrated.env',
    'security.workspace.trust',
    'remote.',
    'http.proxy',
    'extensions.autoupdate',
)
_SETTING_DENY_EXACT = ('git.path', 'npm.packagemanager', 'terminal.explorerkind')
# Matched case-insensitively WITHOUT requiring a leading dot, so
# `python.defaultInterpreterPath` is caught alongside `python.interpreterPath`.
_SETTING_DENY_SUFFIXES = ('executablepath', 'interpreterpath', 'serverpath',
                          'shellargs', 'shellpath', 'runtimeexecutable')
_SETTING_KEY_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.\-\[\]]{0,127}$')
_SETTING_MAX_VALUE_BYTES = 8192


def _setting_denied(key: str) -> str:
    low = key.lower()
    if low in _SETTING_DENY_EXACT:
        return 'redirects the editor at a different binary'
    for p in _SETTING_DENY_PREFIXES:
        if low.startswith(p):
            return 'can run a command or redirect traffic on every editor open'
    for s in _SETTING_DENY_SUFFIXES:
        if low.endswith(s):
            return 'points the editor at an executable path'
    return ''


def normalize_settings(value: Any) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    accepted: Dict[str, Any] = {}
    denied: List[Dict[str, str]] = []
    if not isinstance(value, dict):
        return accepted, denied
    for key, val in value.items():
        if not isinstance(key, str) or not _SETTING_KEY_RE.match(key):
            denied.append({'key': str(key)[:120], 'reason': 'invalid setting key'})
            continue
        reason = _setting_denied(key)
        if reason:
            denied.append({'key': key, 'reason': reason})
            continue
        try:
            encoded = _canonical(val)
        except (TypeError, ValueError):
            denied.append({'key': key, 'reason': 'value is not JSON-serializable'})
            continue
        if len(encoded) > _SETTING_MAX_VALUE_BYTES:
            denied.append({'key': key, 'reason': 'value is too large'})
            continue
        accepted[key] = val
    return accepted, denied


# ── environment ──────────────────────────────────────────────────────────────

# THE HIGHEST-VALUE GUARD IN THIS FILE.
#
# ANTHROPIC_BASE_URL is the sharpest example: a repo setting it would silently
# proxy every agent prompt — including whatever the agent reads from this
# workspace — through an endpoint the repo author controls. LD_PRELOAD and
# GIT_SSH_COMMAND are straight code execution; PATH and BASH_ENV reroute every
# later command; the provider prefixes are credential capture.
_ENV_DENY_EXACT = frozenset({
    'PATH', 'HOME', 'SHELL', 'USER', 'LOGNAME', 'IFS', 'BASH_ENV', 'ENV',
    'PROMPT_COMMAND', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'LD_AUDIT',
    'DYLD_INSERT_LIBRARIES', 'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_EXTERNAL_DIFF',
    'GIT_PAGER', 'GIT_EDITOR', 'GIT_CONFIG', 'GIT_CONFIG_GLOBAL',
    'GIT_PROXY_COMMAND', 'PAGER', 'EDITOR', 'VISUAL', 'NODE_OPTIONS',
    'PYTHONSTARTUP', 'PYTHONPATH', 'PERL5OPT', 'RUBYOPT',
    'http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
    'NPM_CONFIG_REGISTRY', 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL',
})
_ENV_DENY_PREFIXES = ('ANTHROPIC_', 'OPENAI_', 'OPENROUTER_', 'AZURE_', 'AWS_',
                      'GOOGLE_', 'GEMINI_', 'GH_', 'GITHUB_', 'KC_', 'CLAUDE_',
                      'CODEX_', 'HYPERVISOR_', 'LD_', 'BASH_', 'CLOUDSDK_')
_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
_ENV_MAX_VALUE = 4096


def normalize_env(value: Any, workdir: str = '/home/dev'
                  ) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    accepted: Dict[str, str] = {}
    denied: List[Dict[str, str]] = []
    if not isinstance(value, dict):
        return accepted, denied
    for key, val in value.items():
        name = str(key)
        if not _ENV_NAME_RE.match(name):
            denied.append({'key': name[:120], 'reason': 'invalid variable name'})
            continue
        if name in _ENV_DENY_EXACT:
            denied.append({
                'key': name,
                'reason': 'reserved — it would change how every later command '
                          'or agent request runs'})
            continue
        if any(name.startswith(p) for p in _ENV_DENY_PREFIXES):
            denied.append({
                'key': name,
                'reason': 'reserved prefix — these carry workspace credentials '
                          'and agent endpoints'})
            continue
        if not isinstance(val, (str, int, float)) or isinstance(val, bool):
            denied.append({'key': name, 'reason': 'value must be a string'})
            continue
        text, _ = substitute_vars(str(val), workdir)
        if '\n' in text or '\r' in text or '\0' in text:
            denied.append({'key': name, 'reason': 'value contains a newline or NUL'})
            continue
        if len(text) > _ENV_MAX_VALUE:
            denied.append({'key': name, 'reason': 'value is too large'})
            continue
        accepted[name] = text
    return accepted, denied


# ── workspaceFolder ──────────────────────────────────────────────────────────


def map_workspace_folder(workdir: str, value: Any) -> Tuple[str, str]:
    """Map the container-side workspaceFolder onto this pod. (path, caveat).

    Never trusted as a path: the declared value is container-relative
    (`/workspaces/<repo>` under the reference implementation) and this file
    comes from a cloned repo. Anything that does not resolve back inside
    `workdir` is refused, not clamped.
    """
    if value is None:
        return workdir, ''
    if not isinstance(value, str) or not value.strip():
        return workdir, 'workspaceFolder is not a string — using the project directory'
    declared = value.strip()
    base = os.path.basename(os.path.normpath(workdir))
    prefix = '/workspaces/' + base
    if declared == prefix or declared == workdir:
        return workdir, ''
    rel = ''
    if declared.startswith(prefix + '/'):
        rel = declared[len(prefix) + 1:]
    elif declared.startswith(workdir + '/'):
        rel = declared[len(workdir) + 1:]
    elif not declared.startswith('/'):
        rel = declared
    else:
        return workdir, (
            f'workspaceFolder `{declared}` does not map into this workspace — '
            'commands will run in the project directory instead')
    candidate = os.path.normpath(os.path.join(workdir, rel))
    root = os.path.normpath(workdir)
    if candidate != root and not candidate.startswith(root + os.sep):
        return workdir, (
            f'workspaceFolder `{declared}` escapes the project directory — ignored')
    return candidate, ''


# ── unsupported properties ───────────────────────────────────────────────────
#
# Never a generic "unsupported". Every entry carries a reason grounded in this
# pod's actual constraints plus a remedy the user can act on. Three severities:
#   blocking — the environment the file describes cannot be produced at all
#   partial  — we apply part of it; the rest is dropped
#   ignored  — irrelevant here, no impact on the result

_POD_ROOT_REASON = (
    'Dev container features install as root at image-build time. This pod '
    'runs as UID 1000 with allowPrivilegeEscalation=false, so nothing can '
    'write to /usr.')

UNSUPPORTED_KEYS: Dict[str, Tuple[str, str, str]] = {
    'image': ('blocking',
              "The pod's image comes from the Helm chart (image.repository / "
              "image.tag), not from devcontainer.json.",
              'Bake what you need into the workspace image, or install into '
              '$HOME from postCreateCommand.'),
    'build': ('blocking',
              'Building an image needs a Docker daemon. build.mode defaults to '
              'kaniko, so there is none in this pod.',
              'Move the Dockerfile steps into the workspace image, or into '
              'postCreateCommand where they only need $HOME.'),
    'features': ('blocking', _POD_ROOT_REASON,
                 'Anything that installs into $HOME or /home/dev/.local can '
                 'move to postCreateCommand instead.'),
    'dockerComposeFile': ('blocking',
                          'This workspace is a single pod — there are no '
                          'sibling services to compose.',
                          'Run dependencies as processes in the pod (they '
                          'show up on the Apps page), or point at a managed '
                          'service outside the cluster.'),
    'service': ('blocking',
                'Compose service selection has no meaning for a single pod.',
                'Remove it, or keep it for other tools — it is ignored here.'),
    'runServices': ('ignored', 'Compose-only.', 'No action needed.'),
    'overrideFeatureInstallOrder': ('ignored',
                                    'Ordering for features, which cannot run '
                                    'here at all.', 'No action needed.'),
    'mounts': ('partial',
               'Volume mounts are set by the Helm chart at pod creation; a '
               'file inside the repo cannot add one.',
               'The PVC at /home/dev already persists. Ask an operator to '
               'extend the chart if you need another volume.'),
    'runArgs': ('partial',
                'Container run flags are the pod spec here, which the chart '
                'owns.', 'Ask an operator if you need a capability added.'),
    'privileged': ('blocking',
                   'The pod sets allowPrivilegeEscalation=false and drops all '
                   'capabilities — by design, so a cloned repo cannot ask for '
                   'more.', 'Not available in this workspace.'),
    'capAdd': ('blocking',
               'All Linux capabilities are dropped in the pod securityContext.',
               'Not available in this workspace.'),
    'securityOpt': ('blocking', 'Security options are fixed by the pod spec.',
                    'Not available in this workspace.'),
    'hostRequirements': ('partial',
                         'CPU and memory come from the chart '
                         '(resources.requests / limits), not from the repo.',
                         'Ask an operator to raise the workspace limits.'),
    'initializeCommand': ('ignored',
                          'Runs on the CLIENT before the container exists. '
                          'There is no client here — the workspace is the '
                          'machine.', 'Move it to onCreateCommand if it needs '
                          'to run in the workspace.'),
    'postAttachCommand': ('ignored',
                          'Runs each time an editor attaches. This pod is '
                          'long-lived and shared by code-server, ttyd and '
                          'agents, so there is no single attach point.',
                          'Move it to postStartCommand.'),
    'remoteUser': ('partial',
                   'The workspace always runs as the `dev` user (UID 1000).',
                   'No action needed unless the file expects root.'),
    'containerUser': ('partial',
                      'The container user is fixed at UID 1000 by the pod '
                      'securityContext.', 'No action needed.'),
    'updateRemoteUserUID': ('ignored', 'UID mapping is fixed by the chart.',
                            'No action needed.'),
    'shutdownAction': ('ignored',
                       'The pod lifecycle is managed by Kubernetes.',
                       'No action needed.'),
    'waitFor': ('ignored',
                'Applies to the editor attach sequence, which does not exist '
                'here.', 'No action needed.'),
    'userEnvProbe': ('ignored', 'Shell probing is not used.', 'No action needed.'),
    'appPort': ('partial',
                'Host port publishing is the ingress, which the chart owns. '
                'forwardPorts is honoured instead.',
                'List the port under forwardPorts and it appears on the Apps '
                'page.'),
    'otherPortsAttributes': ('ignored',
                             'Only explicitly forwarded ports are pinned.',
                             'List the port under forwardPorts.'),
}


def classify_unsupported(cfg: Dict[str, Any],
                         extra: Optional[List[Dict[str, str]]] = None
                         ) -> List[Dict[str, str]]:
    """Every declared property we cannot honour, with reason and remedy."""
    out: List[Dict[str, str]] = []
    for key, (severity, reason, remedy) in UNSUPPORTED_KEYS.items():
        if key not in cfg:
            continue
        val = cfg[key]
        if val in (None, '', [], {}):
            continue
        if key == 'features':
            detail = ', '.join(sorted(val.keys())) if isinstance(val, dict) \
                else str(val)
        elif isinstance(val, (dict, list)):
            detail = _canonical(val)[:300]
        else:
            detail = str(val)[:300]
        out.append({'key': key, 'severity': severity, 'detail': detail,
                    'reason': reason, 'remedy': remedy})
    # customizations for other tools — named so the user knows we saw them.
    cust = cfg.get('customizations')
    if isinstance(cust, dict):
        for tool in sorted(cust.keys()):
            if tool == 'vscode':
                continue
            out.append({
                'key': f'customizations.{tool}', 'severity': 'ignored',
                'detail': '', 'reason': f'kube-coder does not run {tool}.',
                'remedy': 'Only customizations.vscode is applied (code-server).'})
    out.extend(extra or [])
    order = {'blocking': 0, 'partial': 1, 'ignored': 2}
    out.sort(key=lambda e: (order.get(e['severity'], 3), e['key']))
    return out


# ── parse ────────────────────────────────────────────────────────────────────


def parse(workdir: str) -> Dict[str, Any]:
    """Full read-through record for one workdir. Never raises.

    Nothing here is stored: two stats plus one <=256 KiB read is cheaper than
    the git branch lookup ProjectsManager.brief already does per workdir, and a
    cache would just be a second source of truth to invalidate.
    """
    rec: Dict[str, Any] = {
        'workdir': workdir, 'found': False, 'path': '', 'rel_path': '',
        'error': '', 'error_line': 0, 'error_column': 0,
        'config_hash': '', 'name': '', 'workspace_folder': workdir,
        'lifecycle': {h: [] for h in HOOKS}, 'hook_hashes': {},
        'ports': [], 'ports_skipped': [],
        'extensions': [], 'extensions_rejected': [],
        'settings': {}, 'settings_denied': [],
        'env': {}, 'env_denied': [],
        'unsupported': [], 'caveats': [], 'needs_root': False,
    }
    path = find_config(workdir)
    if not path:
        return rec
    rec['found'] = True
    rec['path'] = path
    rec['rel_path'] = os.path.relpath(path, workdir)
    try:
        raw = read_raw(path)
    except DevcontainerError as e:
        rec['error'] = e.message
        return rec
    rec['config_hash'] = content_hash(raw)
    try:
        cfg = loads_jsonc(raw)
    except DevcontainerError as e:
        rec['error'] = e.message
        rec['error_line'] = e.line
        rec['error_column'] = e.column
        return rec

    name = cfg.get('name')
    rec['name'] = name.strip()[:120] if isinstance(name, str) else ''

    folder, folder_caveat = map_workspace_folder(workdir, cfg.get('workspaceFolder'))
    rec['workspace_folder'] = folder
    if folder_caveat:
        rec['caveats'].append(folder_caveat)

    extra_unsupported: List[Dict[str, str]] = []
    for hook in HOOKS:
        try:
            cmds = normalize_commands(cfg.get(HOOK_KEYS[hook]), folder)
        except DevcontainerError as e:
            cmds = []
            extra_unsupported.append({
                'key': HOOK_KEYS[hook], 'severity': 'blocking', 'detail': '',
                'reason': e.message,
                'remedy': 'Use a string, an array of arguments, or an object '
                          'of named commands.'})
        rec['lifecycle'][hook] = cmds
        rec['hook_hashes'][hook] = hook_hash(cmds)
        for c in cmds:
            for cav in c.get('caveats') or []:
                if cav.startswith('unresolved variables'):
                    rec['caveats'].append(f'{HOOK_KEYS[hook]}: {cav}')
            if c.get('needs_root'):
                rec['needs_root'] = True

    rec['ports'], rec['ports_skipped'] = normalize_ports(
        cfg.get('forwardPorts'), cfg.get('portsAttributes'))

    vscode = ((cfg.get('customizations') or {}).get('vscode')
              if isinstance(cfg.get('customizations'), dict) else None)
    vscode = vscode if isinstance(vscode, dict) else {}
    rec['extensions'], rec['extensions_rejected'] = \
        normalize_extensions(vscode.get('extensions'))
    rec['settings'], rec['settings_denied'] = \
        normalize_settings(vscode.get('settings'))

    merged_env: Dict[str, Any] = {}
    for key in ('containerEnv', 'remoteEnv'):
        if isinstance(cfg.get(key), dict):
            merged_env.update(cfg[key])
    rec['env'], rec['env_denied'] = normalize_env(merged_env, folder)

    if rec['needs_root']:
        extra_unsupported.append({
            'key': 'lifecycle commands needing root', 'severity': 'partial',
            'detail': '', 'reason': _POD_ROOT_REASON,
            'remedy': 'Install into $HOME (nvm, pyenv, rustup, pip --user) or '
                      'bake the package into the workspace image.'})
    rec['unsupported'] = classify_unsupported(cfg, extra_unsupported)
    return rec


def summarize(workdir: str) -> Dict[str, Any]:
    """Counts-only view for the project brief — same read, smaller payload."""
    rec = parse(workdir)
    if not rec['found']:
        return {'found': False}
    if rec['error']:
        return {'found': True, 'path': rec['rel_path'], 'error': rec['error']}
    blocking = sum(1 for u in rec['unsupported'] if u['severity'] == 'blocking')
    return {
        'found': True,
        'path': rec['rel_path'],
        'name': rec['name'],
        'config_hash': rec['config_hash'],
        'ports': len(rec['ports']),
        'extensions': len(rec['extensions']),
        'settings': len(rec['settings']),
        'env': len(rec['env']),
        'hooks': {h: len(rec['lifecycle'][h]) for h in HOOKS
                  if rec['lifecycle'][h]},
        'unsupported': len(rec['unsupported']),
        'blocking': blocking,
        'blocking_keys': [u['key'] for u in rec['unsupported']
                          if u['severity'] == 'blocking'],
        'needs_root': rec['needs_root'],
    }


# ── state ────────────────────────────────────────────────────────────────────
#
# Only what we DID is stored — everything parsed is read-through. One file,
# keyed by workdir rather than project id: a project has N workdirs and a git
# worktree carries its own devcontainer, and slugifying absolute paths into
# per-project filenames would be a needless traversal surface.

STATE_DIR = '/home/dev/.claude-devcontainer'
STATE_VERSION = 1
BOOT_MARKER = '/tmp/.kc-devcontainer-boot'


def state_path() -> str:
    return os.path.join(STATE_DIR, 'state.json')


def log_dir() -> str:
    return os.path.join(STATE_DIR, 'logs')


def log_path_for(workdir: str, hook: str, when: Optional[float] = None) -> str:
    slug = re.sub(r'[^A-Za-z0-9]+', '-', workdir).strip('-')[:80] or 'workspace'
    ts = int(when if when is not None else time.time())
    return os.path.join(log_dir(), f'{slug}.{hook}.{ts}.log')


def ensure_dirs() -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(log_dir(), mode=0o700, exist_ok=True)


def read_state() -> Dict[str, Any]:
    try:
        with open(state_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {'version': STATE_VERSION, 'workdirs': {}}
    if not isinstance(data, dict) or not isinstance(data.get('workdirs'), dict):
        return {'version': STATE_VERSION, 'workdirs': {}}
    data.setdefault('version', STATE_VERSION)
    return data


def write_state(state: Dict[str, Any]) -> None:
    """Atomic tmp+replace at 0600 — a torn state file would make us re-run a
    hook that already succeeded, or skip one that never did."""
    ensure_dirs()
    path = state_path()
    tmp = f'{path}.tmp.{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


# One state file holds every workdir, so read-modify-write has to be serialized:
# the boot pass starts one thread per opted-in workdir, and two of them landing
# their hook results at the same time would silently drop one record. The lock
# is here rather than in DevcontainerManager because get/put are also called
# from request handlers, which are ThreadingHTTPServer workers.
_STATE_LOCK = threading.Lock()


def get_record(workdir: str) -> Dict[str, Any]:
    return (read_state().get('workdirs') or {}).get(workdir) or {}


def put_record(workdir: str, record: Dict[str, Any]) -> None:
    with _STATE_LOCK:
        state = read_state()
        state.setdefault('workdirs', {})[workdir] = record
        write_state(state)


def drop_record(workdir: str) -> bool:
    with _STATE_LOCK:
        state = read_state()
        if workdir in (state.get('workdirs') or {}):
            del state['workdirs'][workdir]
            write_state(state)
            return True
    return False


def boot_id() -> str:
    """Identity of the current pod boot, used to re-run postStart once per start.

    Read-or-create a random token under /tmp. /tmp is container-local and wiped
    on restart, so it IS a per-boot identity by construction. Deliberately not
    /proc/sys/kernel/random/boot_id — that is the NODE's boot id: it survives a
    pod restart on the same node, which would silently make postStart never
    run again.
    """
    try:
        with open(BOOT_MARKER, 'r', encoding='utf-8') as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_hex(8)
    try:
        with open(BOOT_MARKER, 'w', encoding='utf-8') as f:
            f.write(token)
    except OSError:
        # Unwritable /tmp: return a per-call token so postStart is treated as
        # pending rather than silently "done".
        return token
    return token


def lifecycle_status(parsed: Dict[str, Any],
                     record: Optional[Dict[str, Any]] = None,
                     current_boot: Optional[str] = None) -> Dict[str, Any]:
    """Per-hook state: none | pending | done | stale | failed | running.

    Invalidation is by CONTENT HASH, never mtime: `git checkout` and a PVC
    restore both bump mtime with identical content, and a rebase can move it
    backwards.
    """
    record = record or {}
    ran = record.get('lifecycle') or {}
    boot = current_boot if current_boot is not None else boot_id()
    out: Dict[str, Any] = {}
    for hook in HOOKS:
        cmds = (parsed.get('lifecycle') or {}).get(hook) or []
        entry: Dict[str, Any] = {'count': len(cmds)}
        if not cmds:
            entry['status'] = 'none'
            out[hook] = entry
            continue
        want = (parsed.get('hook_hashes') or {}).get(hook) or hook_hash(cmds)
        prev = ran.get(hook) or {}
        entry['ran_at'] = prev.get('ran_at')
        entry['exit_code'] = prev.get('exit_code')
        entry['log_tail'] = prev.get('log_tail', '')
        entry['log_path'] = prev.get('log_path', '')
        if not prev:
            entry['status'] = 'pending'
        elif prev.get('status') == 'running':
            entry['status'] = 'running'
        elif prev.get('hook_hash') != want:
            entry['status'] = 'stale'
        elif prev.get('status') in ('failed', 'timed_out'):
            entry['status'] = prev['status']
        elif hook == 'postStart' and prev.get('boot_id') != boot:
            # New pod boot: postStart is per-start by definition.
            entry['status'] = 'pending'
        else:
            entry['status'] = 'done'
        out[hook] = entry
    return out
