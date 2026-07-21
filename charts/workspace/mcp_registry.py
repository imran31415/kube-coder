#!/usr/bin/env python3
"""User-defined MCP servers, fanned out to every MCP-capable assistant.

Issue #353: the boot scripts seed a fixed set of MCP servers (memory,
agent-orchestrator, dashboard, …) into each assistant's native config, but a
user who wants to ADD a connector had to hand-edit ~/.claude.json from a
terminal — and that only covered Claude. This module is the single canonical
registry behind the dashboard's Settings → MCP servers section:

    /home/dev/.claude-tasks/mcp-servers.json      (PVC, 0600)
    {"servers": {name: {command, args, env, enabled}}, "managed": [name, ...]}

`sync_all()` translates the registry into each provider's native config:

    Claude    ~/.claude.json                → mcpServers.<name> (stdio)
    OpenCode  ~/.config/opencode/opencode.json → mcp.<name> (type=local)
    Ante      ~/.ante/settings.json         → mcp_servers.<name>
    Codex     $CODEX_HOME/config.toml       → via `codex mcp add/remove`
              (the CLI merges TOML for us; best-effort, gated on the binary)

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

REGISTRY_FILE = '/home/dev/.claude-tasks/mcp-servers.json'

# Provider config surfaces. $HOME is /home/ubuntu for both start.sh and the
# dashboard server, matching where the boot seeding writes (~/.claude.json and
# opencode.json are ephemeral and re-seeded each boot; ~/.codex is PVC-backed).
CLAUDE_CONFIG = os.path.expanduser('~/.claude.json')
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


def _validate(name, command, args, env):
    """Returns an error string, or None when the entry is well-formed."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return 'name must match ^[A-Za-z0-9_-]{1,64}$'
    if name in RESERVED_NAMES:
        return f'"{name}" is a built-in workspace MCP server and cannot be replaced'
    if not isinstance(command, str) or not command.strip():
        return 'command is required'
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        return 'args must be a list of strings'
    if not isinstance(env, dict):
        return 'env must be an object'
    for k, v in env.items():
        if not isinstance(k, str) or not ENV_KEY_RE.match(k):
            return f'invalid env var name: {k!r}'
        if not isinstance(v, str):
            return f'env value for {k} must be a string'
    return None


def set_server(name, command, args=None, env=None, enabled=True):
    """Add or update a registry entry. Returns (ok, err).

    Env values may hold secrets, so on an update a BLANK value for an
    already-stored env key keeps the previous value (the UI's redacted view
    round-trips without re-entering secrets) — same rule as
    GatewayCredentialsManager.set(). Blank values for new keys are dropped.
    """
    args = args if args is not None else []
    env = env if env is not None else {}
    err = _validate(name, command, args, env)
    if err:
        return False, err
    reg = _read_registry()
    prev = reg['servers'].get(name)
    prev_env = prev.get('env', {}) if isinstance(prev, dict) else {}
    out_env = {}
    for k, v in env.items():
        v = v.strip()
        if v:
            out_env[k] = v
        elif isinstance(prev_env.get(k), str) and prev_env[k]:
            out_env[k] = prev_env[k]
    reg['servers'][name] = {
        'command': command.strip(),
        'args': [a.strip() for a in args if a.strip()],
        'env': out_env,
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


def public_view():
    """Redacted listing for the UI/API: env VALUES are never returned (they
    may hold API keys) — only a last-4 hint per key, like provider-keys."""
    reg = _read_registry()
    out = []
    for name in sorted(reg['servers']):
        entry = reg['servers'][name]
        if not isinstance(entry, dict):
            continue
        env = entry.get('env') if isinstance(entry.get('env'), dict) else {}
        out.append({
            'name': name,
            'command': entry.get('command', ''),
            'args': list(entry.get('args', [])),
            'env': {k: (f'…{v[-4:]}' if isinstance(v, str) and len(v) >= 4 else '•••')
                    for k, v in env.items()},
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


def _sync_claude(upserts, removals):
    def translate(e):
        out = {'type': 'stdio', 'command': e['command'], 'args': e['args']}
        if e['env']:
            out['env'] = e['env']
        return out
    cfg = _load_json_file(CLAUDE_CONFIG)
    if _apply_map(cfg, 'mcpServers', upserts, removals, translate):
        _atomic_json_write(CLAUDE_CONFIG, cfg)


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

    results = {}
    for provider in _PROVIDERS:
        try:
            results[provider] = globals()[f'_sync_{provider}'](upserts, removals) or 'ok'
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
