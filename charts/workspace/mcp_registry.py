#!/usr/bin/env python3
"""User-defined MCP servers, fanned out to every MCP-capable assistant.

Issue #353: the boot scripts seed a fixed set of MCP servers (memory,
agent-orchestrator, dashboard, …) into each assistant's native config, but a
user who wants to ADD a connector had to hand-edit ~/.claude.json from a
terminal — and that only covered Claude. This module is the single canonical
registry behind the dashboard's Settings → MCP servers section:

    /home/dev/.claude-tasks/mcp-servers.json      (PVC, 0600)
    {"servers": {name: entry}, "managed": [name, ...]}

    stdio entry   {type: stdio, command, args, env, enabled}
    remote entry  {type: http | sse, url, headers, enabled}       (#815)

An entry with no `type` predates #815 and is stdio.

`sync_all()` translates the registry into each provider's native config:

    Claude    ~/.claude.json, BOTH homes  → mcpServers.<name> (stdio/http/sse)
    OpenCode  ~/.config/opencode/opencode.json → mcp.<name> (type=local)
    Ante      ~/.ante/settings.json         → mcp_servers.<name>
    Codex     $CODEX_HOME/config.toml       → via `codex mcp add/remove`
              (the CLI merges TOML for us; best-effort, gated on the binary)

Remote (http/sse) entries are Claude-only for now: OpenCode, Ante and Codex
get only the stdio entries, and a name that turns remote is removed from them.
Their remote equivalents (OpenCode `type: remote`, `codex mcp add --url`) are
not verified here — widen _REMOTE_PROVIDERS once one is. A remote server's
OAuth login lives in ~/.claude/.credentials.json (`mcpOAuth`), which both
homes share through the /home/ubuntu/.claude → /home/dev/.claude symlink, so
the entry itself never holds a secret unless the user adds a header.

LibreFang and kc-harness have no MCP seeding today, so they are out of scope
here — when they grow one, add a `_sync_*` function and list it in _PROVIDERS.

The `managed` list records which names THIS registry has written into the
provider configs, so removal only ever deletes entries we own — the
boot-seeded defaults (memory, agent-orchestrator, dashboard, playwright,
sequential-thinking) and any hand-edits the user made directly in a provider
file are never touched. Those default names are also reserved: the registry
refuses to shadow them.

The Hypervisor's curated 2-server set (hypervisor_session._HYPERVISOR_MCP_CONFIG)
is deliberately NOT extended by this registry — it is pinned for connect-speed
reliability (explicit non-goal of #353).

Every provider sync is independently best-effort: one unreadable config never
blocks the others, and the boot invocation (start.sh) treats failures as
warnings. Runnable as `python3 mcp_registry.py --sync` — start.sh calls this
after the per-provider default seeding so user-added servers are re-applied to
the ephemeral-$HOME configs on every pod boot.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

REGISTRY_FILE = '/home/dev/.claude-tasks/mcp-servers.json'

# Provider config surfaces. $HOME is /home/ubuntu for both start.sh and the
# dashboard server, matching where the boot seeding writes (~/.claude.json and
# opencode.json are ephemeral and re-seeded each boot; ~/.codex is PVC-backed).
#
# Claude gets BOTH homes (#815). A Claude CLI reads $HOME/.claude.json, and
# $HOME is not one value here: Build tasks and sub-agents inherit it from the
# tmux server's global environment (whoever started tmux first), while hooks
# and the Hypervisor are forced to /home/dev. Writing only the server's own
# home left a task launched with the other one without the server's tools.
# Duplicates (same file via a symlink, or the server already in /home/dev)
# are written once.
CLAUDE_CONFIGS = (os.path.expanduser('~/.claude.json'), '/home/dev/.claude.json')
OPENCODE_CONFIG = os.path.expanduser('~/.config/opencode/opencode.json')
ANTE_SETTINGS = os.path.expanduser('~/.ante/settings.json')
CODEX_HOME = '/home/dev/.codex'

# Boot-seeded defaults (seed_claude_config.DESIRED_MCPS + start.sh blocks).
# Reserved so a registry entry can never shadow or delete them.
RESERVED_NAMES = frozenset({
    'memory', 'agent-orchestrator', 'dashboard', 'playwright',
    'sequential-thinking',
})

NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
# RFC 9110 field-name (token) characters.
HEADER_KEY_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")

TRANSPORTS = ('stdio', 'http', 'sse')
REMOTE_TRANSPORTS = frozenset({'http', 'sse'})
# Plain http only to the pod itself — anything else must be https.
LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})
URL_MAX = 2048
# Providers whose sync understands remote entries; the rest get stdio only.
_REMOTE_PROVIDERS = frozenset({'claude'})

CODEX_TIMEOUT = 30  # seconds per `codex mcp` invocation


# ── registry store (ProviderKeysManager discipline: one PVC JSON, 0600) ──

def _read_registry() -> dict:
    try:
        with open(REGISTRY_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {'servers': {}, 'managed': []}
    if not isinstance(data, dict):
        return {'servers': {}, 'managed': []}
    servers = data.get('servers')
    managed = data.get('managed')
    return {
        'servers': servers if isinstance(servers, dict) else {},
        'managed': [n for n in managed if isinstance(n, str)]
        if isinstance(managed, list) else [],
    }


def _write_registry(data: dict) -> None:
    os.makedirs(os.path.dirname(REGISTRY_FILE), mode=0o700, exist_ok=True)
    tmp = REGISTRY_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, REGISTRY_FILE)


def _transport(entry):
    """`stdio` | `http` | `sse` for a stored entry; untyped = stdio (pre-#815)."""
    t = entry.get('type', 'stdio') if isinstance(entry, dict) else 'stdio'
    return t if t in TRANSPORTS else 'stdio'


def _validate_pairs(pairs, key_re, what):
    if not isinstance(pairs, dict):
        return f'{what} must be an object'
    for k, v in pairs.items():
        if not isinstance(k, str) or not key_re.match(k):
            return f'invalid {what} name: {k!r}'
        if not isinstance(v, str):
            return f'{what} value for {k} must be a string'
    return None


def _validate_url(url):
    if not isinstance(url, str) or not url.strip():
        return 'url is required'
    url = url.strip()
    if len(url) > URL_MAX or any(c.isspace() or ord(c) < 0x20 for c in url):
        return 'url must be a single absolute URL'
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return 'url must be a single absolute URL'
    if not host:
        return 'url must be a single absolute URL'
    if parts.username or parts.password:
        # The list view shows the URL back; credentials go in a header.
        return 'url must not embed credentials — use a header instead'
    if parts.scheme == 'https':
        return None
    if parts.scheme == 'http' and host in LOOPBACK_HOSTS:
        return None
    return 'url must be https (plain http only for localhost)'


def _validate(name, command, args, env, transport='stdio', url=None,
              headers=None):
    """Returns an error string, or None when the entry is well-formed."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return 'name must match ^[A-Za-z0-9_-]{1,64}$'
    if name in RESERVED_NAMES:
        return f'"{name}" is a built-in workspace MCP server and cannot be replaced'
    if transport not in TRANSPORTS:
        return 'type must be one of: ' + ', '.join(TRANSPORTS)
    if transport in REMOTE_TRANSPORTS:
        err = _validate_url(url)
        if err:
            return err
        err = _validate_pairs(headers if headers is not None else {},
                              HEADER_KEY_RE, 'header')
        if err:
            return err
        if any(c in '\r\n' for v in (headers or {}).values() for c in v):
            return 'header values must be a single line'
        return None
    if not isinstance(command, str) or not command.strip():
        return 'command is required'
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        return 'args must be a list of strings'
    return _validate_pairs(env, ENV_KEY_RE, 'env var')


def _merge_secrets(new, prev):
    """Strip values; a BLANK value for an already-stored key keeps the stored
    one (the UI's redacted view round-trips without re-entering secrets) —
    same rule as GatewayCredentialsManager.set(). Blank new keys are dropped."""
    prev = prev if isinstance(prev, dict) else {}
    out = {}
    for k, v in new.items():
        v = v.strip()
        if v:
            out[k] = v
        elif isinstance(prev.get(k), str) and prev[k]:
            out[k] = prev[k]
    return out


def set_server(name, command='', args=None, env=None, enabled=True,
               transport=None, url=None, headers=None):
    """Add or update a registry entry. Returns (ok, err).

    `transport` is the API's `type`: stdio (command/args/env) or http/sse
    (url/headers). Omitted, it keeps the stored entry's transport — so an
    older client that only knows stdio can still toggle a remote entry —
    and defaults to stdio for a new one. Likewise on a remote update a blank
    `url` keeps the stored URL and `headers=None` keeps the stored headers.

    Env and header values may hold secrets, so a blank value for an
    already-stored key keeps the previous value (see _merge_secrets).
    """
    reg = _read_registry()
    prev = reg['servers'].get(name) if isinstance(name, str) else None
    prev = prev if isinstance(prev, dict) else None
    if transport is None:
        transport = _transport(prev) if prev else 'stdio'
    same_remote = (prev is not None and transport in REMOTE_TRANSPORTS
                   and _transport(prev) in REMOTE_TRANSPORTS)
    if same_remote:
        if not (isinstance(url, str) and url.strip()):
            url = prev.get('url')
        if headers is None:
            headers = {k: '' for k in prev.get('headers') or {}}
    args = args if args is not None else []
    env = env if env is not None else {}
    err = _validate(name, command, args, env, transport, url, headers)
    if err:
        return False, err
    if transport in REMOTE_TRANSPORTS:
        reg['servers'][name] = {
            'type': transport,
            'url': url.strip(),
            'headers': _merge_secrets(
                headers or {}, prev.get('headers') if same_remote else None),
            'enabled': bool(enabled),
        }
    else:
        reg['servers'][name] = {
            'type': 'stdio',
            'command': command.strip(),
            'args': [a.strip() for a in args if a.strip()],
            'env': _merge_secrets(
                env, prev.get('env') if prev and _transport(prev) == 'stdio'
                else None),
            'enabled': bool(enabled),
        }
    _write_registry(reg)
    return True, None


def delete_server(name):
    """Remove a registry entry. Returns True if it existed. The name stays in
    `managed` until the next sync_all() pass removes it from provider configs.
    """
    reg = _read_registry()
    if name not in reg['servers']:
        return False
    del reg['servers'][name]
    _write_registry(reg)
    return True


def _redact(pairs):
    pairs = pairs if isinstance(pairs, dict) else {}
    return {k: (f'…{v[-4:]}' if isinstance(v, str) and len(v) >= 4 else '•••')
            for k, v in pairs.items()}


def public_view():
    """Redacted listing for the UI/API: env and header VALUES are never
    returned (they may hold API keys) — only a last-4 hint per key, like
    provider-keys. Every row carries the stdio fields (empty for a remote
    entry) so a client that predates #815 still renders it."""
    reg = _read_registry()
    out = []
    for name in sorted(reg['servers']):
        entry = reg['servers'][name]
        if not isinstance(entry, dict):
            continue
        transport = _transport(entry)
        remote = transport in REMOTE_TRANSPORTS
        out.append({
            'name': name,
            'type': transport,
            'command': '' if remote else entry.get('command', ''),
            'args': [] if remote else list(entry.get('args', [])),
            'env': {} if remote else _redact(entry.get('env')),
            'url': entry.get('url', '') if remote else '',
            'headers': _redact(entry.get('headers')) if remote else {},
            # Which assistants this entry reaches (#815: remote = Claude-only).
            'providers': sorted(_REMOTE_PROVIDERS) if remote
            else list(_PROVIDERS),
            'enabled': bool(entry.get('enabled', True)),
        })
    return out


# ── provider fan-out ─────────────────────────────────────────────────────

def _load_json_file(path):
    """Parse a provider config. Raises on invalid JSON (the caller records the
    error and leaves the file alone); missing file → fresh {}."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f'{path} is not a JSON object')
    return data


def _atomic_json_write(path, data):
    parent = os.path.dirname(path) or '.'
    os.makedirs(parent, exist_ok=True)
    tmp = path + '.mcp-tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def _apply_map(cfg, key, upserts, removals, translate):
    """Shared merge: upsert translated entries / drop removals under cfg[key],
    touching only the given names. Returns True if cfg changed."""
    section = cfg.get(key)
    if not isinstance(section, dict):
        section = {}
    changed = False
    for name, entry in upserts.items():
        desired = translate(entry)
        if section.get(name) != desired:
            section[name] = desired
            changed = True
    for name in removals:
        if name in section:
            del section[name]
            changed = True
    if changed:
        cfg[key] = section
    return changed


def _claude_config_paths():
    seen, out = set(), []
    for path in CLAUDE_CONFIGS:
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            out.append(path)
    return out


def _sync_claude(upserts, removals):
    def translate(e):
        transport = _transport(e)
        if transport in REMOTE_TRANSPORTS:
            out = {'type': transport, 'url': e['url']}
            if e.get('headers'):
                out['headers'] = e['headers']
            return out
        out = {'type': 'stdio', 'command': e['command'], 'args': e['args']}
        if e['env']:
            out['env'] = e['env']
        return out
    # Each home's file is independent: a corrupt one is left alone and
    # reported, and the other is still written.
    failures = []
    for path in _claude_config_paths():
        try:
            cfg = _load_json_file(path)
            if _apply_map(cfg, 'mcpServers', upserts, removals, translate):
                _atomic_json_write(path, cfg)
        except Exception as e:
            failures.append(f'{path}: {e}')
    if failures:
        raise RuntimeError('; '.join(failures))


def _sync_opencode(upserts, removals):
    # OpenCode: {type: local, command: [argv...], enabled, environment}.
    # start.sh regenerates opencode.json from scratch on boot, then re-runs
    # this sync — so registry entries survive the regeneration.
    def translate(e):
        out = {'type': 'local', 'command': [e['command']] + e['args'],
               'enabled': True}
        if e['env']:
            out['environment'] = e['env']
        return out
    cfg = _load_json_file(OPENCODE_CONFIG)
    if not cfg:
        cfg = {'$schema': 'https://opencode.ai/config.json'}
    if _apply_map(cfg, 'mcp', upserts, removals, translate):
        _atomic_json_write(OPENCODE_CONFIG, cfg)


def _sync_ante(upserts, removals):
    def translate(e):
        out = {'command': e['command'], 'args': e['args']}
        if e['env']:
            out['env'] = e['env']
        return out
    cfg = _load_json_file(ANTE_SETTINGS)
    if _apply_map(cfg, 'mcp_servers', upserts, removals, translate):
        _atomic_json_write(ANTE_SETTINGS, cfg)


def _sync_codex(upserts, removals):
    # Codex owns its TOML; drive it through the CLI like start.sh does.
    # `codex mcp add` overwrites the named server and preserves the rest.
    # An absent binary (older image) is a skip, not a failure — there is
    # nothing to configure, and treating it as an error would pin removal
    # retries forever on codex-less pods.
    if not shutil.which('codex'):
        return 'skipped: codex CLI not installed'
    env = dict(os.environ, CODEX_HOME=CODEX_HOME)
    failures = []
    for name, entry in upserts.items():
        argv = ['codex', 'mcp', 'add', name]
        for k, v in entry['env'].items():
            argv += ['--env', f'{k}={v}']
        argv += ['--', entry['command']] + entry['args']
        r = subprocess.run(argv, env=env, capture_output=True,
                           timeout=CODEX_TIMEOUT)
        if r.returncode != 0:
            failures.append(f'add {name}')
    for name in removals:
        # `remove` of a never-added name may fail; that's fine — the goal
        # (name absent from config.toml) is met either way.
        subprocess.run(['codex', 'mcp', 'remove', name], env=env,
                       capture_output=True, timeout=CODEX_TIMEOUT)
    if failures:
        raise RuntimeError('codex mcp ' + ', '.join(failures) + ' failed')


# Looked up via globals() at sync time (not captured here) so tests can patch
# an individual _sync_* function on the module.
# NB: the DeepSeek Harness (#639) is deliberately NOT here. Every provider
# above owns an on-disk config file this registry can merge servers into; the
# harness's MCP surface is per-SESSION instead — ACP's `session/new` takes the
# server list as a request field, so a file-based fan-out has nothing to write.
# Its servers are supplied by the ACP bridge at session open. Adding a no-op
# entry here would only make sync_all() report a provider it never syncs.
_PROVIDERS = ('claude', 'opencode', 'ante', 'codex')


def sync_all():
    """Fan the registry out to every provider config. Each provider is
    independently best-effort; returns {provider: 'ok' | 'error: …'}.

    Removals = names we previously managed that are now deleted or disabled.
    Reserved names are structurally excluded (set_server refuses them), so a
    removal can never delete a boot-seeded default.
    """
    reg = _read_registry()
    upserts = {n: e for n, e in reg['servers'].items()
               if isinstance(e, dict) and e.get('enabled', True)
               and n not in RESERVED_NAMES}
    known = set(reg['managed']) | set(reg['servers'])
    removals = sorted((known - set(upserts)) - RESERVED_NAMES)

    # Remote entries reach only _REMOTE_PROVIDERS; for the rest they count as
    # removals, so a server that switched stdio → http leaves those configs.
    stdio = {n: e for n, e in upserts.items() if _transport(e) == 'stdio'}
    stdio_removals = sorted(set(removals) | (set(upserts) - set(stdio)))

    results = {}
    for provider in _PROVIDERS:
        if provider in _REMOTE_PROVIDERS:
            ups, rems = upserts, removals
        else:
            ups, rems = stdio, stdio_removals
        try:
            results[provider] = globals()[f'_sync_{provider}'](ups, rems) or 'ok'
        except Exception as e:  # one provider must never block the others
            results[provider] = f'error: {e}'
            print(f'[mcp-registry] {provider} sync failed: {e}',
                  file=sys.stderr)

    # Managed = the names currently fanned out. Names whose removal failed on
    # some provider stay listed so the next pass retries the cleanup.
    failed = {p for p, r in results.items() if r.startswith('error')}
    if failed and removals:
        managed = sorted(set(upserts) | set(removals))
    else:
        managed = sorted(upserts)
    if managed != reg['managed']:
        reg['managed'] = managed
        _write_registry(reg)
    return results


def main(argv):
    if '--sync' not in argv:
        print('usage: mcp_registry.py --sync', file=sys.stderr)
        return 2
    results = sync_all()
    for provider, result in sorted(results.items()):
        print(f'[mcp-registry] {provider}: {result}')
    # Best-effort by design: boot must not fail because one CLI is absent.
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
