"""Isolated git worktrees for Builds (#701).

One git worktree + one `kc/<slug>` branch + one leased port per agent, so
parallel Builds, Board items and sub-agents stop sharing a checkout, an index,
a branch and port 3000. This module is the ONE implementation: server.py
imports it, mcp_agent_orchestrator.py imports it, and the `worktree` skill's
shell script delegates to its CLI. Three copies of a port-lease algorithm is
how two of them end up leasing the same port.

Stdlib only, no `server` import — it ships as a top-level file the Dockerfile
already copies to /tmp/browser, and it must work inside the orchestrator's
stdio subprocess where server.py is not loaded.

Design rules, each the alternative to something that looks simpler and is
wrong:

- **One cross-process lock** (`<root>/.kc-worktrees.lock`, `flock`) around the
  cap count, the port lease, `git worktree add`, the manifest write and
  removal. The board driver thread, an HTTP request, an orchestrator process
  and a human running the shell skill can all create at once; a lease that is
  "lowest port nobody has claimed yet" is only correct if the claim and the
  write happen inside one lock.
- **Nothing half-created.** Every failure after `worktree add` rolls back the
  directory, and deletes the branch only if THIS call created it — through a
  compare-and-swap `update-ref -d <ref> <old>`, so a branch that moved in the
  meantime is never lost.
- **A removal never deletes a branch.** Only the sweep does, and only a branch
  we created whose tip is still exactly its base: it holds nothing unique.
- **git runs hardened.** argv only (never a shell), hooks disabled (a repo's
  post-checkout hook must not run inside the server), `--no-optional-locks`
  (a status poll must never take the index lock out from under the agent's
  `git commit`), no external diff drivers, a bounded timeout on every call, and
  secret-looking variables stripped from the environment.
- **`_RUN` is captured at import.** Tests patch `server.subprocess.run` to stub
  tmux; that patch replaces the attribute on the shared `subprocess` module, and
  would silently stub git too. Holding our own reference keeps git real.
"""

import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:          # pragma: no cover - a Windows dev box
    fcntl = None

# Captured at import — see the module docstring.
_RUN = subprocess.run

API_VERSION = 1
MANIFEST = '.kc-worktree.json'
# Files this feature writes INTO a worktree. They are excluded from git and
# ignored when deciding whether a tree is dirty — otherwise every worktree is
# "dirty" from birth and nothing could ever be removed without --force.
OWN_FILES = (MANIFEST, '.kc-issue-prompt.md')
LOCK_NAME = '.kc-worktrees.lock'
REPO_MARKER = '.kc-repo'
BRANCH_PREFIX = 'kc/'
SLUG_MAX = 40
MANIFEST_VERSION = 2
HISTORY_MAX = 10

# kube-coder's in-pod ports (server.py AppsManager.INTERNAL_PORTS). Pinned to
# that set by a test rather than imported, because this module must not import
# server.
RESERVED_PORTS = frozenset({22, 2376, 5900, 6080, 6081, 7681, 8080})

DEFAULT_HOME = '/home/dev'

# ── error codes ───────────────────────────────────────────────────────────
# Grouped by how a caller should answer them. server.py maps these to HTTP
# statuses; the CLI maps them to exit codes. Every code is a stable string a
# UI can switch on — never parse `message`.

#: The request is wrong: a path, a ref, a repo that cannot be isolated.
INVALID_CODES = frozenset({
    'outside_home', 'not_dir', 'not_git', 'home_is_repo', 'empty_repo',
    'bare_repo', 'bad_ref', 'unknown_ref', 'bad_slug', 'inside_root',
})
#: The workspace is full. Try again after cleaning up.
REJECT_CODES = frozenset({'worktree_cap', 'no_port'})
#: Something else holds what this request needs.
CONFLICT_CODES = frozenset({'busy', 'path_conflict', 'branch_in_use'})
#: Removal refusals.
REMOVE_CODES = frozenset({'live', 'dirty', 'repo_missing', 'not_worktree'})


class WorktreeError(Exception):
    """A refusal with a stable `code` and a message fit to show a human."""

    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self):
        out = {'error': self.message, 'code': self.code}
        out.update(self.detail)
        return out


# ── configuration ─────────────────────────────────────────────────────────

def _int_env(name, default, lo=None):
    try:
        v = int(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        v = default
    if lo is not None and v < lo:
        v = default
    return v


def default_root():
    return os.environ.get('KC_WORKTREE_ROOT') or os.path.join(DEFAULT_HOME, '.worktrees')


def port_range():
    """`[lo, hi]`. 3100 rather than 3000 at the bottom: an UN-isolated dev
    server still defaults to 3000, and a lease must not hand that out."""
    lo = _int_env('KC_WT_PORT_LO', 3100, lo=1)
    hi = _int_env('KC_WT_PORT_HI', 3999, lo=1)
    if hi < lo:
        lo, hi = 3100, 3999
    return lo, min(hi, 65535)


def git_timeout():
    return _int_env('KC_GIT_TIMEOUT', 15, lo=1)


def git_add_timeout():
    return _int_env('KC_GIT_ADD_TIMEOUT', 180, lo=1)


def lock_timeout():
    return _int_env('KC_WT_LOCK_TIMEOUT', 60, lo=1)


# ── git ───────────────────────────────────────────────────────────────────

# Variables that would point git at a DIFFERENT repository than the one we
# name with -C. A server started from inside a git hook inherits these.
_GIT_REDIRECT_ENV = (
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
    'GIT_NAMESPACE', 'GIT_PREFIX',
)
_SECRET_ENV = re.compile(
    r'(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|PRIVATE_KEY|'
    r'^ANTHROPIC_|^OPENAI_|^GH_|^GITHUB_|^AWS_)', re.I)


def _git_env():
    env = {k: v for k, v in os.environ.items()
           if k not in _GIT_REDIRECT_ENV and not _SECRET_ENV.search(k)}
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['LC_ALL'] = 'C'
    return env


def _git(args, cwd, *, timeout=None, check=True, binary=False):
    """Run one git command. Returns the CompletedProcess.

    Raises WorktreeError('git_timeout' | 'git_missing' | 'git_failed').
    `check=False` hands back a non-zero result for the caller to judge — used
    where git's exit status is an answer (`show-ref --verify`) not a failure.
    """
    argv = ['git', '--no-optional-locks',
            '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.fsmonitor=false',
            '-c', 'core.quotePath=false',
            '-C', cwd] + list(args)
    kwargs = {'capture_output': True, 'env': _git_env(),
              'timeout': timeout or git_timeout()}
    if not binary:
        kwargs.update(text=True, encoding='utf-8', errors='replace')
    try:
        proc = _RUN(argv, **kwargs)
    except subprocess.TimeoutExpired:
        raise WorktreeError('git_timeout',
                            f'git {args[0]} timed out after '
                            f'{kwargs["timeout"]}s')
    except FileNotFoundError:
        raise WorktreeError('git_missing', 'git is not installed')
    if check and proc.returncode != 0:
        err = proc.stderr if not binary else proc.stderr.decode('utf-8', 'replace')
        raise WorktreeError('git_failed',
                            f'git {args[0]} failed: {(err or "").strip()[:400]}')
    return proc


def _out(args, cwd, **kw):
    return (_git(args, cwd, **kw).stdout or '').strip()


# ── names ─────────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$')
_SHA_RE = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
# A base ref is a branch, a remote branch, a tag, a SHA or HEAD~N. Deliberately
# narrow: no ':' (tree:path syntax), no '{' (reflog syntax), no leading '-'
# (an option), no '..' (a range). Anything wider is a thing a caller did not
# mean to be able to say.
_REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/~^-]{0,199}$')


def slugify(text, maxlen=SLUG_MAX):
    """Lowercase, runs of anything but [a-z0-9] become '-', trimmed, capped.
    Mirrors worktree.sh's `slugify` exactly (a test holds them together)."""
    s = re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')
    return s[:maxlen].strip('-')


def valid_slug(slug):
    return isinstance(slug, str) and bool(_SLUG_RE.fullmatch(slug))


def board_slug(board_id, item_id):
    """One slug per (board, item) — so one branch per ticket, and a send-back
    lands on the same path.

    The hash is what makes it safe: item ids are vendor-shaped (GitHub GraphQL
    ids carry ':' and '=', `A:1` and `a-1` slugify identically) and a slug
    collision between two tickets would hand one ticket's worktree to the
    other. At most 2+14+1+12+1+8 = 38 characters.
    """
    digest = hashlib.sha1(f'{board_id}\0{item_id}'.encode('utf-8')).hexdigest()[:8]
    b = slugify(board_id, 14) or 'x'
    i = slugify(item_id, 12) or 'x'
    return f'b-{b}-{i}-{digest}'


def validate_ref_text(ref):
    if (not isinstance(ref, str) or not _REF_RE.fullmatch(ref) or '..' in ref
            or '//' in ref or ref.endswith(('/', '.', '.lock'))):
        raise WorktreeError('bad_ref',
                            f'{ref!r} is not a branch, tag or commit this can '
                            f'branch from')


def branch_for(slug):
    return BRANCH_PREFIX + slug


# ── paths ─────────────────────────────────────────────────────────────────

def _real(path):
    return os.path.realpath(path)


def _inside(child, parent):
    child, parent = _real(child), _real(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def confine(path, home_root=DEFAULT_HOME):
    """Realpath of `path`, which must be a directory inside `home_root`.
    Realpath-compared with a separator, so neither `/home/dev/../etc` nor a
    lookalike sibling like `/home/devious` passes."""
    if not path or not isinstance(path, str):
        raise WorktreeError('not_dir', 'a working directory is required')
    real = _real(path)
    if not _inside(real, home_root):
        raise WorktreeError('outside_home',
                            f'{path} is not inside {home_root}')
    if not os.path.isdir(real):
        raise WorktreeError('not_dir', f'{path} is not a directory')
    return real


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix='.kc-wt-', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_manifest(path):
    """The manifest in worktree `path`, defaults filled for v1 files written
    by the pre-#701 shell script. None when there is no readable manifest."""
    data = _read_json(os.path.join(path, MANIFEST))
    if data is None:
        return None
    data.setdefault('version', 1)
    data.setdefault('slug', os.path.basename(path.rstrip(os.sep)))
    data.setdefault('branch', branch_for(data['slug']))
    data.setdefault('task_id', '')
    data.setdefault('base_ref', '')
    data.setdefault('base_sha', '')
    data.setdefault('created_by', 'worktree.sh' if data['version'] < 2 else '')
    data.setdefault('branch_created', False)
    data.setdefault('history', [])
    if not data.get('created_at'):
        try:
            data['created_at'] = os.stat(path).st_mtime
        except OSError:
            data['created_at'] = 0
    return data


# ── the lock ──────────────────────────────────────────────────────────────

# In-process serialisation on top of flock. flock alone already excludes two
# threads (each call opens its own file description), but a platform without
# a working fcntl would otherwise have no lock at all.
_THREAD_LOCK = threading.Lock()


def real_flock():
    """Whether flock actually locks here (it is a no-op shim on a Windows dev
    box). Mutual-exclusion tests skip when this is False."""
    if fcntl is None or getattr(fcntl, '_kube_coder_shim', False):
        return False
    return getattr(fcntl.flock, '__module__', '') == 'fcntl'


class _Locked:
    def __init__(self, root, timeout=None):
        self.root = root
        self.timeout = lock_timeout() if timeout is None else timeout
        self._fd = None
        self._held_thread = False

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        if not _THREAD_LOCK.acquire(timeout=max(0.0, self.timeout)):
            raise WorktreeError('lock_timeout',
                                'another worktree operation is still running; '
                                'try again in a moment')
        self._held_thread = True
        try:
            os.makedirs(self.root, exist_ok=True)
            self._fd = os.open(os.path.join(self.root, LOCK_NAME),
                               os.O_RDWR | os.O_CREAT, 0o644)
            if fcntl is not None:
                delay = 0.05
                while True:
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as e:
                        if e.errno not in (errno.EAGAIN, errno.EACCES,
                                           errno.EWOULDBLOCK):
                            raise
                        if time.monotonic() >= deadline:
                            raise WorktreeError(
                                'lock_timeout',
                                'another worktree operation is still '
                                'running; try again in a moment')
                        time.sleep(delay)
                        delay = min(delay * 2, 0.5)
        except BaseException:
            self._release()
            raise
        return self

    def _release(self):
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None
        if self._held_thread:
            self._held_thread = False
            _THREAD_LOCK.release()

    def __exit__(self, *exc):
        self._release()
        return False


def locked(root=None, timeout=None):
    return _Locked(root or default_root(), timeout)


# ── repositories ──────────────────────────────────────────────────────────

def _worktree_list(root):
    """`[{path, head, branch, bare, detached, prunable}]` from porcelain."""
    out = _git(['worktree', 'list', '--porcelain'], root).stdout or ''
    entries, cur = [], None
    for line in out.splitlines():
        if line.startswith('worktree '):
            cur = {'path': _real(line[len('worktree '):]), 'head': '',
                   'branch': '', 'bare': False, 'detached': False}
            entries.append(cur)
        elif cur is None:
            continue
        elif line.startswith('HEAD '):
            cur['head'] = line[5:]
        elif line.startswith('branch '):
            cur['branch'] = line[len('branch '):]
        elif line == 'bare':
            cur['bare'] = True
        elif line == 'detached':
            cur['detached'] = True
    return entries


def resolve_repo(workdir, *, home_root=DEFAULT_HOME, wt_root=None):
    """Everything needed to isolate a Build launched in `workdir`.

    Returns `{root, toplevel, common_dir, subdir, head_sha}`:
    - `root` is the MAIN checkout even when `workdir` is itself a linked
      worktree (a sub-agent spawned from an isolated parent), because every
      worktree hangs off the main repository and the layout keys on it;
    - `subdir` is where `workdir` sits inside its checkout, so a Build started
      in `app/web` starts in `<worktree>/web`, not at the repo root;
    - `head_sha` is the workdir's own HEAD — the default base.
    """
    path = confine(workdir, home_root)
    probe = _git(['rev-parse', '--show-toplevel'], path, check=False)
    if probe.returncode != 0:
        bare = _git(['rev-parse', '--is-bare-repository'], path, check=False)
        if bare.returncode == 0 and (bare.stdout or '').strip() == 'true':
            raise WorktreeError('bare_repo',
                                f'{workdir} is a bare repository; pick a '
                                f'checkout instead')
        raise WorktreeError('not_git',
                            f'{workdir} is not inside a git repository, so it '
                            f'cannot be isolated. Pick a git folder, or turn '
                            f'isolation off.')
    toplevel = _real(probe.stdout.strip())
    common = _real(_out(['rev-parse', '--path-format=absolute',
                         '--git-common-dir'], path))
    entries = _worktree_list(path)
    main = entries[0] if entries else None
    if main is None or main['bare']:
        raise WorktreeError('bare_repo',
                            f'{workdir} belongs to a bare repository; isolate '
                            f'from a regular checkout instead')
    root = main['path']
    if _real(root) == _real(home_root):
        raise WorktreeError('home_is_repo',
                            f'{home_root} is itself a git repository; pick the '
                            f'project folder inside it')
    if not _inside(root, home_root):
        raise WorktreeError('outside_home',
                            f'the repository for {workdir} is not inside '
                            f'{home_root}')
    if wt_root and _inside(root, wt_root):
        raise WorktreeError('inside_root',
                            f'{root} is inside the worktree area {wt_root}')
    head = _git(['rev-parse', '--verify', '-q', 'HEAD^{commit}'], path,
                check=False)
    if head.returncode != 0:
        raise WorktreeError('empty_repo',
                            f'{toplevel} has no commits yet; make a first '
                            f'commit before isolating a Build in it')
    subdir = os.path.relpath(path, toplevel)
    if subdir == os.curdir:
        subdir = ''
    return {'root': root, 'toplevel': toplevel, 'common_dir': common,
            'subdir': subdir.replace(os.sep, '/'),
            'head_sha': head.stdout.strip()}


def resolve_base(repo, base_ref=None, base_sha=None):
    """`(base_ref, base_sha)`. A recorded SHA wins (a send-back rebuilds from
    exactly the commit the original started from); then a named ref; then the
    workdir's own HEAD."""
    root = repo['root']
    if base_sha:
        if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
            raise WorktreeError('bad_ref', f'{base_sha!r} is not a commit id')
        probe = _git(['rev-parse', '--verify', '-q', '--end-of-options',
                      f'{base_sha}^{{commit}}'], root, check=False)
        if probe.returncode == 0:
            return (base_ref or base_sha), probe.stdout.strip()
        if not base_ref:
            raise WorktreeError('unknown_ref',
                                f'commit {base_sha[:12]} is not in this '
                                f'repository any more')
    if base_ref:
        validate_ref_text(base_ref)
        probe = _git(['rev-parse', '--verify', '-q', '--end-of-options',
                      f'{base_ref}^{{commit}}'], root, check=False)
        if probe.returncode != 0:
            raise WorktreeError('unknown_ref',
                                f'{base_ref!r} does not name a commit in '
                                f'{root}')
        return base_ref, probe.stdout.strip()
    return 'HEAD', repo['head_sha']


def _repo_key(wt_root, root):
    """The directory under `wt_root` that holds this repository's worktrees.

    Keyed on the MAIN checkout. The basename alone collides — `/home/dev/app`
    and `/home/dev/clients/app` would share `app/` and hand each other's
    worktrees out — so a `.kc-repo` marker records which root owns a key, and
    a second root with the same name gets a hashed key.
    """
    name = slugify(os.path.basename(root.rstrip(os.sep)), 40) or 'repo'
    hashed = f'{name[:33]}-{hashlib.sha1(root.encode("utf-8")).hexdigest()[:6]}'
    for key in (name, hashed):
        d = os.path.join(wt_root, key)
        if os.path.islink(d):
            continue
        marker = os.path.join(d, REPO_MARKER)
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
            with open(marker, 'w', encoding='utf-8') as f:
                f.write(root + '\n')
            return key
        if not os.path.isdir(d):
            continue
        try:
            with open(marker, encoding='utf-8') as f:
                owner = f.read().strip()
        except OSError:
            owner = None
        if owner is not None:
            if _real(owner) == _real(root):
                return key
            continue
        # A key directory without a marker predates it (the shell skill made
        # it). Adopt it only if nothing in it belongs to another repository.
        roots = {_real(m.get('source_root') or '') for m in _manifests_in(d)}
        roots.discard(_real(''))
        if not roots or all(_same_repo(r, root) for r in roots):
            with open(marker, 'w', encoding='utf-8') as f:
                f.write(root + '\n')
            return key
    raise WorktreeError('path_conflict',
                        f'the worktree folders for {root} are taken by another '
                        f'repository')


def _same_repo(a, b):
    """Whether checkouts `a` and `b` share a repository (one may be a linked
    worktree of the other)."""
    if _real(a) == _real(b):
        return True
    try:
        ca = _out(['rev-parse', '--path-format=absolute', '--git-common-dir'], a)
        cb = _out(['rev-parse', '--path-format=absolute', '--git-common-dir'], b)
    except WorktreeError:
        return False
    return _real(ca) == _real(cb)


def _manifests_in(key_dir):
    out = []
    try:
        names = sorted(os.listdir(key_dir))
    except OSError:
        return out
    for name in names:
        if name.startswith('.'):
            continue
        p = os.path.join(key_dir, name)
        if os.path.islink(p) or not os.path.isdir(p):
            continue
        m = read_manifest(p)
        if m is not None:
            m['path'] = _real(p)
            out.append(m)
    return out


def list_all(wt_root=None):
    """Every worktree this feature (or the shell skill) made, newest first.
    Pure file reads — no git — so it is cheap enough for a list endpoint."""
    wt_root = wt_root or default_root()
    out = []
    try:
        keys = sorted(os.listdir(wt_root))
    except OSError:
        return out
    for key in keys:
        if key.startswith('.'):
            continue
        d = os.path.join(wt_root, key)
        if os.path.islink(d) or not os.path.isdir(d):
            continue
        for m in _manifests_in(d):
            m['repo_key'] = key
            out.append(m)
    out.sort(key=lambda m: m.get('created_at') or 0, reverse=True)
    return out


def find(wt_root, slug, repo_root=None):
    """The manifest for `slug`, preferring `repo_root`'s key. None if absent.
    Raises `path_conflict` when the slug is ambiguous across repositories."""
    hits = [m for m in list_all(wt_root) if m.get('slug') == slug]
    if repo_root:
        mine = [m for m in hits if _real(m.get('source_root') or '') == _real(repo_root)]
        if mine:
            return mine[0]
    if len(hits) > 1:
        raise WorktreeError('path_conflict',
                            f'slug {slug!r} exists in more than one repository: '
                            + ', '.join(m['path'] for m in hits))
    return hits[0] if hits else None


# ── ports ─────────────────────────────────────────────────────────────────

def listen_ports():
    """Ports with a LISTEN socket on any address, from /proc (no ss/netstat
    dependency). Empty where /proc is absent."""
    ports = set()
    for name in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(name, encoding='ascii', errors='replace') as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) > 3 and parts[3] == '0A':
                        try:
                            ports.add(int(parts[1].rsplit(':', 1)[1], 16))
                        except (ValueError, IndexError):
                            pass
        except OSError:
            continue
    return ports


def _pick_port(wt_root, *, reserved=RESERVED_PORTS, rng=None, listening=None):
    lo, hi = rng or port_range()
    taken = set(reserved)
    taken.update(int(m['port']) for m in list_all(wt_root)
                 if isinstance(m.get('port'), int))
    taken.update(listen_ports() if listening is None else listening)
    for p in range(lo, hi + 1):
        if p not in taken:
            return p
    raise WorktreeError('no_port',
                        f'no free port in {lo}-{hi}; remove finished worktrees '
                        f'in Settings → Worktrees')


def free_port(wt_root=None, **kw):
    """A port nobody leases or listens on — WITHOUT leasing it."""
    return _pick_port(wt_root or default_root(), **kw)


# ── ensure ────────────────────────────────────────────────────────────────

def _branch_exists(root, branch):
    return _git(['show-ref', '--verify', '--quiet', f'refs/heads/{branch}'],
                root, check=False).returncode == 0


def _add_excludes(common_dir):
    """Keep our own files out of `git status` and commits. `info/exclude`
    lives in the common dir, so one write covers every worktree."""
    info = os.path.join(common_dir, 'info')
    path = os.path.join(info, 'exclude')
    try:
        os.makedirs(info, exist_ok=True)
        try:
            with open(path, encoding='utf-8') as f:
                have = set(f.read().splitlines())
        except OSError:
            have = set()
        missing = [n for n in OWN_FILES if n not in have]
        if missing:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(''.join(f'{n}\n' for n in missing))
    except OSError:
        pass            # the dirty checks filter OWN_FILES regardless


def _cas_delete_branch(root, branch, expected_sha):
    """Delete `branch` only if it still points at `expected_sha`. Returns
    True when deleted. A branch that moved keeps its new commits."""
    if not expected_sha:
        return False
    proc = _git(['update-ref', '-d', f'refs/heads/{branch}', expected_sha],
                root, check=False)
    return proc.returncode == 0


def _cwd_for(path, subdir):
    if subdir:
        candidate = os.path.join(path, *subdir.split('/'))
        if os.path.isdir(candidate):
            return candidate
    return path


def ensure(repo, slug, *, wt_root=None, base_ref=None, base_sha=None,
           task_id='', created_by='server', max_worktrees=None,
           is_owner_live=None, allow_owner='', reclaim=None,
           reserved=RESERVED_PORTS, rng=None, listening=None, timeout=None):
    """Create — or reuse — the worktree `slug` for `repo`. Returns a dict:

        {path, cwd, slug, branch, port, repo_root, repo_key, subdir,
         base_ref, base_sha, created, branch_created, reused}

    Idempotent by design, because three different callers need the same
    answer from it (a fresh Build, a send-back that must land on the SAME path
    so `--resume` finds its transcript, a re-run of an edited ticket):

    1. the path is our worktree already → reuse it (its port and base stay);
    2. the directory is gone but `kc/<slug>` survives → re-add it from the
       branch, commits intact;
    3. neither → a new branch from the base SHA, `--no-track` so an agent's
       bare `git push` can never be aimed at the base branch.

    `created` / `branch_created` describe what THIS call made; `rollback()`
    undoes exactly that and nothing more. `reclaim(pristine_only=True)` is
    called (inside the lock) when the cap is reached, before refusing.
    """
    wt_root = wt_root or default_root()
    if not valid_slug(slug):
        raise WorktreeError('bad_slug', f'{slug!r} is not a usable worktree name')
    branch = branch_for(slug)
    root = repo['root']
    info = None
    with locked(wt_root, timeout):
        key = _repo_key(wt_root, root)
        key_dir = os.path.join(wt_root, key)
        path = os.path.join(key_dir, slug)
        if os.path.islink(key_dir) or os.path.islink(path):
            raise WorktreeError('path_conflict', f'{path} is a symlink')
        # Clears "already checked out" entries for directories that vanished
        # (deleted by hand, lost with a pod) so they cannot block us.
        _git(['worktree', 'prune'], root, check=False)
        registered = {e['path']: e for e in _worktree_list(root)}
        real_path = _real(path)

        if os.path.exists(path):
            manifest = read_manifest(path)
            if real_path in registered and manifest is not None:
                owner = manifest.get('task_id') or ''
                if (owner and owner != task_id and owner != allow_owner
                        and is_owner_live is not None and is_owner_live(owner)):
                    raise WorktreeError(
                        'busy',
                        f'worktree {slug} is in use by Build {owner}',
                        owner_task_id=owner, path=real_path)
                if task_id and owner != task_id:
                    hist = [h for h in (manifest.get('history') or []) if h]
                    if owner:
                        hist.append(owner)
                    manifest['history'] = hist[-HISTORY_MAX:]
                    manifest['task_id'] = task_id
                manifest['last_used_at'] = time.time()
                _write_json(os.path.join(path, MANIFEST), manifest)
                return {
                    'path': real_path, 'cwd': _cwd_for(real_path, repo.get('subdir')),
                    'slug': slug, 'branch': manifest.get('branch') or branch,
                    'port': manifest.get('port'), 'repo_root': root,
                    'repo_key': key, 'subdir': repo.get('subdir', ''),
                    'base_ref': manifest.get('base_ref') or 'HEAD',
                    'base_sha': manifest.get('base_sha') or '',
                    'created': False, 'branch_created': False, 'reused': True,
                }
            try:
                empty = os.path.isdir(path) and not os.listdir(path)
            except OSError:
                empty = False
            if empty and real_path not in registered:
                os.rmdir(path)          # a leftover of an interrupted create
            else:
                raise WorktreeError(
                    'path_conflict',
                    f'{path} already exists and is not a worktree this '
                    f'workspace manages; move it aside first', path=real_path)

        if max_worktrees:
            count = len(list_all(wt_root))
            if count >= max_worktrees and reclaim is not None:
                try:
                    reclaim()
                except WorktreeError:
                    pass
                count = len(list_all(wt_root))
            if count >= max_worktrees:
                raise WorktreeError(
                    'worktree_cap',
                    f'worktree limit reached ({count}/{max_worktrees}). Remove '
                    f'finished worktrees in Settings → Worktrees, or raise '
                    f'KC_MAX_WORKTREES.', count=count, max=max_worktrees)

        port = _pick_port(wt_root, reserved=reserved, rng=rng, listening=listening)

        branch_created = False
        created = False
        rec_ref = rec_sha = ''
        try:
            if _branch_exists(root, branch):
                holder = next((p for p, e in registered.items()
                               if e.get('branch') == f'refs/heads/{branch}'), None)
                if holder:
                    raise WorktreeError(
                        'branch_in_use',
                        f'branch {branch} is already checked out at {holder}',
                        path=holder)
                tip = _out(['rev-parse', f'refs/heads/{branch}'], root)
                if base_sha and _SHA_RE.fullmatch(base_sha):
                    rec_ref, rec_sha = (base_ref or base_sha), base_sha
                else:
                    mb = _git(['merge-base', tip, repo['head_sha']], root, check=False)
                    rec_sha = (mb.stdout or '').strip() if mb.returncode == 0 else tip
                    rec_ref = base_ref or 'HEAD'
                created = True
                _git(['worktree', 'add', path, branch], root,
                     timeout=git_add_timeout())
            else:
                rec_ref, rec_sha = resolve_base(repo, base_ref, base_sha)
                created = True
                _git(['worktree', 'add', '--no-track', '-b', branch, path, rec_sha],
                     root, timeout=git_add_timeout())
                branch_created = True
            manifest = {
                'version': MANIFEST_VERSION, 'slug': slug, 'repo': key,
                'path': real_path, 'branch': branch, 'port': port,
                'source_root': root, 'task_id': task_id or '',
                'base_ref': rec_ref, 'base_sha': rec_sha,
                'created_at': time.time(), 'created_by': created_by,
                'branch_created': branch_created, 'history': [],
            }
            _write_json(os.path.join(path, MANIFEST), manifest)
            _add_excludes(repo.get('common_dir') or os.path.join(root, '.git'))
        except BaseException as e:
            # `path` did not exist before this call (checked above), so
            # anything there now is ours to take away.
            _rollback_locked(root, path, created=created, branch=branch,
                             branch_created=branch_created, base_sha=rec_sha)
            if isinstance(e, WorktreeError) and e.code == 'git_failed':
                raise _classify_add_failure(e, branch, path)
            raise
        info = {
            'path': real_path, 'cwd': _cwd_for(real_path, repo.get('subdir')),
            'slug': slug, 'branch': branch, 'port': port, 'repo_root': root,
            'repo_key': key, 'subdir': repo.get('subdir', ''),
            'base_ref': rec_ref, 'base_sha': rec_sha,
            'created': True, 'branch_created': branch_created, 'reused': False,
        }
    return info


def _classify_add_failure(err, branch, path):
    msg = err.message.lower()
    if 'already checked out' in msg or 'is already used by worktree' in msg:
        return WorktreeError('branch_in_use',
                             f'branch {branch} is already checked out elsewhere')
    if 'already exists' in msg:
        return WorktreeError('path_conflict', f'{path} already exists')
    return err


def _rollback_locked(root, path, *, created, branch, branch_created, base_sha):
    if created:
        _git(['worktree', 'remove', '--force', path], root, check=False)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        _git(['worktree', 'prune'], root, check=False)
    if branch_created:
        _cas_delete_branch(root, branch, base_sha)
    _forget_status(path)


def rollback(info, *, wt_root=None, timeout=None):
    """Undo exactly what one `ensure()` call made. A reused worktree is left
    alone — rolling back a failed launch must never take away work that
    existed before it."""
    if not info or not info.get('created'):
        return False
    with locked(wt_root or default_root(), timeout):
        _rollback_locked(info['repo_root'], info['path'], created=True,
                         branch=info['branch'],
                         branch_created=bool(info.get('branch_created')),
                         base_sha=info.get('base_sha') or '')
    return True


# ── status / diff ─────────────────────────────────────────────────────────

_STATUS_CACHE = {}
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE_MAX = 64


def _status_ttl():
    return _int_env('KC_WT_STATUS_TTL', 5, lo=0)


def _forget_status(path):
    with _STATUS_CACHE_LOCK:
        for k in [k for k in _STATUS_CACHE if k[0] == _real(path)]:
            _STATUS_CACHE.pop(k, None)


def _porcelain(path):
    """`{path: 'XY'}` of changed and untracked files, own files filtered."""
    raw = _git(['status', '--porcelain=v1', '-z', '--untracked-files=normal',
                '--no-renames'], path).stdout or ''
    out = {}
    for rec in raw.split('\0'):
        if len(rec) < 4:
            continue
        xy, name = rec[:2], rec[3:]
        if name in OWN_FILES:
            continue
        out[name] = xy
    return out


def _is_named_ref(ref):
    return bool(ref) and ref != 'HEAD' and not _SHA_RE.fullmatch(ref) \
        and not ref.startswith('HEAD')


def status(path, *, base_sha, base_ref='', max_files=500, use_cache=True):
    """What changed in worktree `path` since `base_sha` — committed AND
    uncommitted, because a reviewer cares what the agent did, not what it got
    round to committing.

    Git-backed; callers on a hot path read the snapshot kept in task.json
    instead. Cached for KC_WT_STATUS_TTL (5s) so a tab that re-renders does
    not re-run git.
    """
    real = _real(path)
    if not os.path.isdir(real):
        raise WorktreeError('missing', f'{path} no longer exists')
    key = (real, base_sha, base_ref, max_files)
    ttl = _status_ttl()
    now = time.monotonic()
    if use_cache and ttl:
        with _STATUS_CACHE_LOCK:
            hit = _STATUS_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return dict(hit[1])

    porcelain = _porcelain(real)
    head = _git(['rev-parse', '--verify', '-q', 'HEAD'], real, check=False)
    head_sha = (head.stdout or '').strip() if head.returncode == 0 else ''
    sym = _git(['symbolic-ref', '-q', '--short', 'HEAD'], real, check=False)
    branch = (sym.stdout or '').strip() if sym.returncode == 0 else ''

    ahead = behind = None
    base_ok = bool(base_sha) and _git(
        ['rev-parse', '--verify', '-q', f'{base_sha}^{{commit}}'], real,
        check=False).returncode == 0
    files = {}
    if base_ok and head_sha:
        ahead = int(_out(['rev-list', '--count', f'{base_sha}..HEAD'], real) or 0)
        if _is_named_ref(base_ref):
            probe = _git(['rev-parse', '--verify', '-q', '--end-of-options',
                          f'{base_ref}^{{commit}}'], real, check=False)
            if probe.returncode == 0:
                behind = int(_out(['rev-list', '--count',
                                   f'HEAD..{probe.stdout.strip()}'], real) or 0)
        names = _git(['diff', '--name-status', '-z', '--no-renames',
                      '--no-ext-diff', '--no-textconv', base_sha, '--'],
                     real).stdout or ''
        parts = names.split('\0')
        for i in range(0, len(parts) - 1, 2):
            st, name = parts[i], parts[i + 1]
            if name and name not in OWN_FILES:
                files[name] = {'path': name, 'status': st[:1] or 'M',
                               'added': None, 'deleted': None, 'binary': False}
        nums = _git(['diff', '--numstat', '-z', '--no-renames', '--no-ext-diff',
                     '--no-textconv', base_sha, '--'], real).stdout or ''
        for rec in nums.split('\0'):
            cols = rec.split('\t', 2)
            if len(cols) != 3 or cols[2] not in files:
                continue
            f = files[cols[2]]
            if cols[0] == '-' or cols[1] == '-':
                f['binary'] = True
            else:
                f['added'], f['deleted'] = int(cols[0]), int(cols[1])
    for name, xy in porcelain.items():
        if xy == '??' and name not in files:
            files[name] = {'path': name, 'status': '?', 'added': None,
                           'deleted': None, 'binary': False}
    for name, f in files.items():
        f['uncommitted'] = name in porcelain

    ordered = sorted(files.values(), key=lambda f: f['path'])
    dirty = sum(1 for xy in porcelain.values() if xy != '??')
    untracked = sum(1 for xy in porcelain.values() if xy == '??')
    result = {
        'branch': branch, 'head_sha': head_sha, 'detached': not branch,
        'ahead': ahead, 'behind': behind, 'dirty': dirty, 'untracked': untracked,
        'files_changed': len(ordered),
        'insertions': sum(f['added'] or 0 for f in ordered),
        'deletions': sum(f['deleted'] or 0 for f in ordered),
        'files': ordered[:max_files], 'truncated': len(ordered) > max_files,
        'base_known': base_ok, 'computed_at': time.time(),
    }
    if use_cache and ttl:
        with _STATUS_CACHE_LOCK:
            if len(_STATUS_CACHE) >= _STATUS_CACHE_MAX:
                _STATUS_CACHE.pop(next(iter(_STATUS_CACHE)), None)
            _STATUS_CACHE[key] = (now, dict(result))
    return result


def stat_snapshot(st):
    """The small, list-safe summary of a `status()` result."""
    return {k: st.get(k) for k in ('files_changed', 'insertions', 'deletions',
                                   'ahead', 'dirty', 'untracked', 'branch')} | {
        'at': st.get('computed_at') or time.time()}


def diff(path, *, base_sha, file, max_bytes=512 * 1024):
    """Unified diff of ONE file against the base. The file must be one
    `status()` reports as changed — this is a diff viewer, not a file reader,
    and the name is passed after `--` so it can never be read as an option."""
    real = _real(path)
    st = status(real, base_sha=base_sha, max_files=100000)
    entry = next((f for f in st['files'] if f['path'] == file), None)
    if entry is None:
        raise WorktreeError('not_changed',
                            f'{file!r} has no changes in this worktree')
    if entry['status'] == '?':
        proc = _git(['diff', '--no-index', '--no-color', '--no-ext-diff', '--',
                     os.devnull, file], real, check=False, binary=True)
    else:
        proc = _git(['diff', '--no-color', '--no-ext-diff', '--no-textconv',
                     base_sha, '--', file], real, binary=True)
    data = proc.stdout or b''
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode('utf-8', 'replace')
    binary = entry['binary'] or text.lstrip().startswith('Binary files') \
        or '\nBinary files ' in text
    return {'file': file, 'diff': text, 'truncated': truncated, 'binary': binary,
            'status': entry['status']}


# ── remove ────────────────────────────────────────────────────────────────

def unique_unpushed(root, branch):
    """Commits on `branch` that no remote-tracking ref contains. A repository
    with no remotes counts every commit — conservative on purpose: the sweep
    must never mistake "nowhere to push" for "already pushed"."""
    proc = _git(['rev-list', '--count', f'refs/heads/{branch}', '--not',
                 '--remotes'], root, check=False)
    if proc.returncode != 0:
        return 0
    try:
        return int((proc.stdout or '0').strip() or 0)
    except ValueError:
        return 0


def _remove_locked(wt_root, path, *, force, is_owner_live=None, allow_owner=''):
    real = _real(path)
    if not _inside(real, wt_root) or real == _real(wt_root) or os.path.islink(path):
        raise WorktreeError('not_worktree', f'{path} is not a managed worktree')
    manifest = read_manifest(real)
    if manifest is None:
        if not os.path.exists(real):
            return {'removed': False, 'already': True, 'path': real}
        raise WorktreeError('not_worktree', f'{path} is not a managed worktree')
    owner = manifest.get('task_id') or ''
    if (owner and owner != allow_owner and is_owner_live is not None
            and is_owner_live(owner)):
        raise WorktreeError('live',
                            f'Build {owner} is still running in this worktree; '
                            f'stop it first', owner_task_id=owner)
    root = manifest.get('source_root') or ''
    branch = manifest.get('branch') or ''
    repo_ok = bool(root) and os.path.isdir(root) and _git(
        ['rev-parse', '--git-dir'], root, check=False).returncode == 0
    if not repo_ok:
        if not force:
            raise WorktreeError('repo_missing',
                                f'the repository {root or "?"} for this '
                                f'worktree is gone; remove with force to '
                                f'delete the folder')
        shutil.rmtree(real, ignore_errors=True)
        _forget_status(real)
        return {'removed': True, 'already': False, 'path': real,
                'branch': branch, 'branch_kept': True, 'repo_missing': True}

    dirty_n = untracked_n = 0
    if not force:
        porcelain = _porcelain(real)
        dirty_n = sum(1 for xy in porcelain.values() if xy != '??')
        untracked_n = sum(1 for xy in porcelain.values() if xy == '??')
        if dirty_n or untracked_n:
            raise WorktreeError(
                'dirty',
                f'this worktree has {dirty_n} uncommitted and {untracked_n} '
                f'untracked file(s); commit them, or remove with force to '
                f'discard them', dirty=dirty_n, untracked=untracked_n)

    saved = {}
    for name in OWN_FILES:
        p = os.path.join(real, name)
        try:
            with open(p, 'rb') as f:
                saved[name] = f.read()
            os.unlink(p)
        except OSError:
            pass
    args = ['worktree', 'remove'] + (['--force'] if force else []) + [real]
    proc = _git(args, root, check=False)
    if proc.returncode != 0 and not force and 'submodule' in (proc.stderr or '').lower():
        # git refuses a worktree with initialised submodules without --force
        # even when it is clean; our own check above already said it is.
        proc = _git(['worktree', 'remove', '--force', real], root, check=False)
    if proc.returncode != 0:
        for name, data in saved.items():
            try:
                with open(os.path.join(real, name), 'wb') as f:
                    f.write(data)
            except OSError:
                pass
        raise WorktreeError('git_failed',
                            f'git worktree remove failed: '
                            f'{(proc.stderr or "").strip()[:400]}')
    _git(['worktree', 'prune'], root, check=False)
    _forget_status(real)
    return {'removed': True, 'already': False, 'path': real, 'branch': branch,
            'branch_kept': True,
            'delete_branch_command': (f'git -C {shlex.quote(root)} branch -D '
                                      f'{shlex.quote(branch)}') if branch else ''}


def remove(path, *, wt_root=None, force=False, is_owner_live=None,
           allow_owner='', timeout=None):
    """Remove worktree `path`. Refuses while its owner runs (even with
    `force` — pulling the directory out from under a live agent is not a
    cleanup), and refuses a dirty tree unless `force`. Never deletes the
    branch: the commits on it are the reason the worktree existed."""
    wt_root = wt_root or default_root()
    with locked(wt_root, timeout):
        return _remove_locked(wt_root, path, force=force,
                              is_owner_live=is_owner_live,
                              allow_owner=allow_owner)


# ── sweep ─────────────────────────────────────────────────────────────────

TERMINAL_STATUSES = frozenset({'completed', 'killed', 'error'})


def _owners(m):
    """Every task that ever ran in a worktree — the ones whose task.json
    should learn that it is gone."""
    ids = [m.get('task_id') or ''] + list(m.get('history') or [])
    return {'task_id': m.get('task_id') or '',
            'task_ids': [t for t in dict.fromkeys(ids) if t]}


def _sweep_locked(wt_root, *, is_owner_live, owner_meta, now, gc_days, grace_s,
                  dry_run=False, pristine_only=False,
                  delete_pristine_branches=True):
    removed, kept = [], []
    roots = set()

    def keep(m, reason):
        kept.append({'path': m['path'], 'slug': m.get('slug'),
                     'repo_key': m.get('repo_key'), 'reason': reason})

    for m in list_all(wt_root):
        age = now - (m.get('created_at') or 0)
        owner = m.get('task_id') or ''
        if age < grace_s:
            keep(m, 'recent')
            continue
        if owner and is_owner_live(owner):
            keep(m, 'live')
            continue
        finished = m.get('created_at') or 0
        if owner:
            meta = owner_meta(owner)
            if meta is not None:
                if meta.get('status') not in TERMINAL_STATUSES:
                    keep(m, 'not_terminal')
                    continue
                finished = (meta.get('finished_at') or meta.get('killed_at')
                            or meta.get('created_at') or finished)
        root = m.get('source_root') or ''
        if not root or not os.path.isdir(root):
            keep(m, 'repo_missing')
            continue
        try:
            st = status(m['path'], base_sha=m.get('base_sha') or '',
                        use_cache=False)
        except WorktreeError:
            keep(m, 'error')
            continue
        if st['dirty'] or st['untracked']:
            keep(m, 'dirty')
            continue
        branch = m.get('branch') or ''
        base = m.get('base_sha') or ''
        tip = _git(['rev-parse', '--verify', '-q', f'refs/heads/{branch}'],
                   root, check=False)
        tip_sha = (tip.stdout or '').strip() if tip.returncode == 0 else ''
        pristine = bool(base) and st['head_sha'] == base and tip_sha in ('', base)
        old = now - finished >= gc_days * 86400
        if pristine and (owner or old):
            if not dry_run:
                try:
                    _remove_locked(wt_root, m['path'], force=False,
                                   is_owner_live=is_owner_live)
                except WorktreeError as e:
                    keep(m, e.code)
                    continue
                branch_deleted = False
                if (delete_pristine_branches and m.get('branch_created')
                        and tip_sha == base):
                    branch_deleted = _cas_delete_branch(root, branch, base)
                roots.add(root)
            else:
                branch_deleted = False
            removed.append(dict(_owners(m), path=m['path'], slug=m.get('slug'),
                                repo_key=m.get('repo_key'), reason='pristine',
                                branch_deleted=branch_deleted))
            continue
        if pristine_only:
            keep(m, 'not_pristine')
            continue
        if not old:
            keep(m, 'recent')
            continue
        if unique_unpushed(root, branch) > 0:
            keep(m, 'unpushed')
            continue
        if not dry_run:
            try:
                _remove_locked(wt_root, m['path'], force=False,
                               is_owner_live=is_owner_live)
            except WorktreeError as e:
                keep(m, e.code)
                continue
            roots.add(root)
        removed.append(dict(_owners(m), path=m['path'], slug=m.get('slug'),
                            repo_key=m.get('repo_key'), reason='pushed',
                            branch_deleted=False))
    for root in roots:
        _git(['worktree', 'prune'], root, check=False)
    return {'removed': removed, 'kept': kept, 'at': now, 'dry_run': dry_run}


def sweep(*, wt_root=None, is_owner_live, owner_meta, now=None, gc_days=7,
          grace_s=600, dry_run=False, pristine_only=False,
          delete_pristine_branches=True, timeout=None):
    """Remove worktrees nobody needs. Never removes one that is live, recent,
    dirty, or holds a commit no remote has.

    - **pristine** (clean, HEAD and branch still at the base): nothing to
      lose, so it goes as soon as its owner has finished — branch too, if we
      created it (compare-and-swap, so a branch that moved is kept);
    - **pushed** (clean, every commit on some remote, owner finished more than
      `gc_days` ago): the directory goes, the branch stays;
    - everything else is kept, with a reason Settings can show.

    Worktrees with no owner (made by hand with the shell skill) are only
    removed when pristine AND older than `gc_days`.
    """
    wt_root = wt_root or default_root()
    with locked(wt_root, timeout):
        return _sweep_locked(
            wt_root, is_owner_live=is_owner_live, owner_meta=owner_meta,
            now=time.time() if now is None else now, gc_days=gc_days,
            grace_s=grace_s, dry_run=dry_run, pristine_only=pristine_only,
            delete_pristine_branches=delete_pristine_branches)


def reclaimer(wt_root, *, is_owner_live, owner_meta, grace_s=600, gc_days=7):
    """A zero-argument callable for `ensure(reclaim=...)`: a pristine-only
    sweep that runs INSIDE the lock `ensure` already holds."""
    def _reclaim():
        return _sweep_locked(wt_root, is_owner_live=is_owner_live,
                             owner_meta=owner_meta, now=time.time(),
                             gc_days=gc_days, grace_s=grace_s,
                             pristine_only=True)
    return _reclaim


# ── push helpers ──────────────────────────────────────────────────────────

def push_remote(root):
    """The remote a push should go to: `fork` when there is one (a workspace
    identity usually cannot push to `origin`), else `origin`, else the first."""
    proc = _git(['remote'], root, check=False)
    names = [n.strip() for n in (proc.stdout or '').splitlines() if n.strip()]
    for pref in ('fork', 'origin'):
        if pref in names:
            return pref
    return names[0] if names else None


def push_command(path, branch, remote):
    if not remote or not branch:
        return None
    return (f'git -C {shlex.quote(path)} push -u {shlex.quote(remote)} '
            f'{shlex.quote(branch)}')


# ── default owner probes (CLI / orchestrator) ─────────────────────────────

def _tasks_dir():
    home = os.environ.get('KC_WORKSPACE_HOME') or DEFAULT_HOME
    return os.path.join(home, '.claude-tasks')


def default_owner_meta(task_id):
    if not task_id or not re.match(r'^[A-Za-z0-9_-]+$', task_id):
        return None
    return _read_json(os.path.join(_tasks_dir(), task_id, 'task.json'))


def default_is_owner_live(task_id):
    """A task is live while its tmux session exists. Builds run as
    `kube-coder-<id>`, orchestrator sub-agents as `claude-<id>`."""
    if not task_id or not re.match(r'^[A-Za-z0-9_-]+$', task_id):
        return False
    meta = default_owner_meta(task_id) or {}
    names = [meta.get('tmux_session')] if meta.get('tmux_session') else []
    names += [f'kube-coder-{task_id}', f'claude-{task_id}']
    for name in names:
        try:
            proc = _RUN(['tmux', 'has-session', '-t', f'={name}'],
                        capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if proc.returncode == 0:
            return True
    return False


# ── CLI ───────────────────────────────────────────────────────────────────

def _emit(obj, as_json):
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))


def main(argv=None):
    """`python3 worktrees.py <command> ...` — the shell skill delegates here.

    Exit status: 0 ok, 2 usage, 3 a refusal (message on stderr; with --json the
    refusal is also printed as JSON on stdout).
    """
    import argparse
    p = argparse.ArgumentParser(prog='worktrees.py')
    p.add_argument('--api-version', action='store_true')
    sub = p.add_subparsers(dest='cmd')

    e = sub.add_parser('ensure')
    e.add_argument('--repo', default=os.getcwd())
    e.add_argument('--slug', default='')
    e.add_argument('--base', default='')
    e.add_argument('--task-id', default=os.environ.get('KC_TASK_ID', ''))
    e.add_argument('--created-by', default='cli')
    e.add_argument('--emit-shell', action='store_true')
    e.add_argument('--json', action='store_true')

    s = sub.add_parser('status')
    s.add_argument('--path', default=os.getcwd())
    s.add_argument('--json', action='store_true')

    r = sub.add_parser('remove')
    r.add_argument('--slug', default='')
    r.add_argument('--path', default='')
    r.add_argument('--force', action='store_true')
    r.add_argument('--json', action='store_true')

    ls = sub.add_parser('list')
    ls.add_argument('--repo', default='')
    ls.add_argument('--json', action='store_true')

    sub.add_parser('port')

    sw = sub.add_parser('sweep')
    sw.add_argument('--dry-run', action='store_true')
    sw.add_argument('--gc-days', type=int,
                    default=_int_env('KC_WORKTREE_GC_DAYS', 7, lo=0))
    sw.add_argument('--json', action='store_true')

    args = p.parse_args(argv)
    if args.api_version:
        print(API_VERSION)
        return 0
    wt_root = default_root()
    home = os.environ.get('KC_WORKSPACE_HOME') or DEFAULT_HOME
    as_json = bool(getattr(args, 'json', False))
    try:
        if args.cmd == 'ensure':
            repo = resolve_repo(args.repo, home_root=home, wt_root=wt_root)
            slug = slugify(args.slug or args.task_id or
                           f'{time.strftime("%H%M%S")}-{os.getpid()}')
            if not slug:
                raise WorktreeError('bad_slug', 'empty slug')
            info = ensure(
                repo, slug, wt_root=wt_root, base_ref=args.base or None,
                task_id=args.task_id, created_by=args.created_by,
                max_worktrees=_int_env('KC_MAX_WORKTREES', 20, lo=1),
                is_owner_live=default_is_owner_live,
                allow_owner=os.environ.get('KC_TASK_ID', ''),
                reclaim=reclaimer(wt_root, is_owner_live=default_is_owner_live,
                                  owner_meta=default_owner_meta))
            if args.emit_shell:
                print(f'worktree ready:\n  path    {info["path"]}\n'
                      f'  branch  {info["branch"]}\n  port    {info["port"]}   '
                      f'(preview: /api/app-proxy/{info["port"]}/)\n'
                      f'  cd {shlex.quote(info["cwd"])}', file=sys.stderr)
                print(f'export KC_WT={shlex.quote(info["path"])}')
                print(f'export KC_WT_BRANCH={shlex.quote(info["branch"])}')
                print(f'export PORT={info["port"]} KC_PORT={info["port"]}')
            else:
                _emit(info, True)
        elif args.cmd == 'status':
            m = read_manifest(_real(args.path)) or {}
            _emit(status(args.path, base_sha=m.get('base_sha') or '',
                         base_ref=m.get('base_ref') or ''), True)
        elif args.cmd == 'remove':
            if args.path:
                target = args.path
            else:
                slug = slugify(args.slug)
                root = None
                try:
                    root = resolve_repo(os.getcwd(), home_root=home)['root']
                except WorktreeError:
                    pass
                m = find(wt_root, slug, root) if slug else None
                if m is None:
                    raise WorktreeError('not_worktree',
                                        f'no worktree with slug {args.slug!r}')
                target = m['path']
            out = remove(target, wt_root=wt_root, force=args.force,
                         is_owner_live=default_is_owner_live,
                         allow_owner=os.environ.get('KC_TASK_ID', ''))
            if as_json:
                _emit(out, True)
            else:
                print(f'removed {out["path"]}', file=sys.stderr)
                if out.get('branch'):
                    print(f"branch '{out['branch']}' KEPT — delete when done: "
                          f"{out.get('delete_branch_command')}", file=sys.stderr)
        elif args.cmd == 'list':
            items = list_all(wt_root)
            if args.repo:
                root = resolve_repo(args.repo, home_root=home)['root']
                items = [m for m in items
                         if _real(m.get('source_root') or '') == _real(root)]
            if as_json:
                _emit(items, True)
            else:
                for m in items:
                    print(f"  {m.get('slug')}\tport {m.get('port')}\t"
                          f"branch {m.get('branch')}\t{m['path']}")
        elif args.cmd == 'port':
            print(free_port(wt_root))
        elif args.cmd == 'sweep':
            _emit(sweep(wt_root=wt_root, is_owner_live=default_is_owner_live,
                        owner_meta=default_owner_meta, gc_days=args.gc_days,
                        grace_s=_int_env('KC_WORKTREE_GRACE_S', 600, lo=0),
                        dry_run=args.dry_run), True)
        else:
            p.print_help(sys.stderr)
            return 2
    except WorktreeError as err:
        print(f'worktree: {err.message}', file=sys.stderr)
        if as_json:
            _emit(err.as_dict(), True)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
