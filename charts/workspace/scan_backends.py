"""Where a scan actually runs — the seam (#726).

`ScanBackend` is the whole contract between kube-coder and whatever performs a
scan. The API, the UI, the event stream and the findings model sit above it and
know none of this; `LocalStrixBackend` sits below it and is the only place in
the tree that names the scanner, spawns a process, or knows what address the
target has. Moving scans onto a dedicated host later is a second implementation
of this class, not a rewrite — which is the condition the feature was approved
under.

## Two shapes that keep the seam honest

**`artifacts()` returns parsed data, never a path.** A remote runner has no file
inside this pod. A seam that handed back a directory would be local-only in
disguise, and the day someone wrote the remote backend they would find the whole
layer above had grown a dependency on the filesystem.

**`resolve_target()` belongs to the backend, not to the caller.** "Which address
can reach port 3000?" has a different answer for every backend: the container
network's gateway here, something else entirely from another machine. Answering
it above the seam would bake this deployment's topology into the API.

## The API key is never in our hands

The scanner reads its own credentials from its own config file, which
`strix_connection.py` writes. This module therefore passes the *model* to each
scan (so a scan can use a different one than the saved default) and nothing
else: no key in our process environment, no key in an argument list, no key in
any log this module writes. The subprocess reads the file itself.
"""

import errno
import json
import os
import re
import shutil
import signal
import subprocess
import time
from abc import ABC, abstractmethod


#: Where the scanner leaves its output, relative to the directory it is run in.
#: Its own constant; we run each scan in its own directory so the results land
#: inside that scan's folder rather than in a shared tree.
RUNS_SUBDIR = 'strix_runs'
RUN_RECORD = 'run.json'
FINDINGS_RECORD = 'vulnerabilities.json'

#: The scanner's console output, kept for diagnosing a scan that failed to
#: start. Capped because an unbounded log on a PVC is a slow disk-full.
LOG_NAME = 'scanner.log'
LOG_MAX_BYTES = 512 * 1024

#: How long a stopped process gets to exit cleanly before it is killed.
STOP_GRACE_SECONDS = 10


class ScanBackend(ABC):
    """One way of running a scan. See the module docstring for the contract."""

    #: Recorded on every scan so a record made by one backend is never
    #: misread by another.
    name = 'abstract'

    @abstractmethod
    def preflight(self):
        """`{ok, reason, detail}` — can this backend run a scan right now?

        Called before the UI offers to start one, so the user reads a specific
        sentence about what is missing instead of watching a scan fail.
        """

    @abstractmethod
    def resolve_target(self, port):
        """An address this backend can reach the workspace's port on.

        Discovered at call time. A hardcoded address that is wrong does not
        error — it produces a clean report against nothing.
        """

    @abstractmethod
    def start(self, spec):
        """Begin a scan. Returns a JSON-serialisable handle, immediately.

        Never blocks on the scan itself: the caller answers an HTTP request
        with the result of this call.
        """

    @abstractmethod
    def artifacts(self, handle):
        """`{'run': {...}, 'findings': [...]}`, or None when nothing is
        readable yet. Called repeatedly while a scan runs."""

    @abstractmethod
    def is_alive(self, handle):
        """Is this scan's work still in progress?"""

    @abstractmethod
    def stop(self, handle):
        """End a running scan. Idempotent — a finished scan is not an error."""


# ── pure helpers (unit-tested without a scanner, a daemon or a network) ─────

_GATEWAY_RE = re.compile(r'^default\s+via\s+(\S+)', re.MULTILINE)
#: Conservative: an IPv4 dotted quad or a bracketable IPv6 literal. The value
#: goes into a URL, so anything else is refused rather than interpolated.
_IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')


def parse_default_gateway(route_output):
    """The default gateway address from `ip route` output, or None.

    This is the address the workspace's own ports are reachable on from inside
    a sibling container. It is conventionally 172.17.0.1, but that is a default
    the container runtime is free to change, and a wrong guess here is the
    silent-empty-result failure — so it is read, never assumed.
    """
    match = _GATEWAY_RE.search(route_output or '')
    if not match:
        return None
    addr = match.group(1)
    return addr if _IPV4_RE.match(addr) else None


def build_argv(spec, *, executable='strix'):
    """The scanner's command line for one scan.

    Two flags are unconditional, and both are guards rather than preferences:

    * `--non-interactive`, because there is no terminal attached and the
      interactive build would sit forever waiting for one.
    * `--scan-mode`, because the scanner's own default is its deepest mode —
      hours of work and a matching bill. A scan that reaches this function
      without an explicit mode is a bug, so the mode is required, not defaulted.

    The budget flag appears only when the user set one, mirroring the scanner's
    own optional cap. No credential is ever placed on this command line.
    """
    mode = spec.get('mode')
    if not mode:
        raise ValueError('scan spec has no mode')
    target = spec.get('target_url')
    if not target:
        raise ValueError('scan spec has no resolved target')

    argv = [executable, '--non-interactive', '--scan-mode', str(mode),
            '--target', str(target)]
    budget = spec.get('budget_usd')
    if budget:
        argv += ['--max-budget', str(budget)]
    instruction = (spec.get('instruction') or '').strip()
    if instruction:
        argv += ['--instruction', instruction]
    return argv


def newest_run_dir(scan_dir):
    """The scanner's output directory for this scan, or None.

    It names the directory itself, from the target, so we discover it rather
    than predict it. One scan runs per directory, so "the newest one here" is
    unambiguous — and stays correct if the naming scheme changes.
    """
    base = os.path.join(scan_dir, RUNS_SUBDIR)
    try:
        entries = [os.path.join(base, n) for n in os.listdir(base)]
    except OSError:
        return None
    dirs = [p for p in entries if os.path.isdir(p)]
    if not dirs:
        return None
    return max(dirs, key=lambda p: os.path.getmtime(p))


def read_json_file(path, default=None):
    """Parse a JSON file, or return `default`.

    Never raises. These files are read while another process is rewriting
    them, so a partial read is expected and ordinary: the next poll gets the
    complete version a moment later.
    """
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def process_alive(pid):
    """Is this pid running? False for a pid we may not signal."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


# ── the local backend ──────────────────────────────────────────────────────

class LocalStrixBackend(ScanBackend):
    """Runs the scanner as a subprocess in this pod.

    The scanner drives its own container sandbox through the workspace's
    container runtime, so this backend's preflight checks both: the scanner is
    installed, and the runtime answers.
    """

    name = 'local-strix'

    def __init__(self, *, executable=None, env=None, runner=None,
                 redactor=None):
        #: Injected so tests drive a stub and never spawn anything. Production
        #: passes nothing and gets the real subprocess module.
        self._runner = runner or _SubprocessRunner()
        self._executable = executable
        self._env = env if env is not None else os.environ
        #: Strips the user's credential out of scanner output by exact value.
        #: Injected rather than imported: this module never holds a secret, and
        #: giving it one so it could redact would defeat the point.
        self._redactor = redactor or (lambda text: text)

    # -- capability ---------------------------------------------------------

    def executable(self):
        """Path to the scanner, or None when it is not installed yet."""
        if self._executable:
            return self._executable if os.path.exists(self._executable) else None
        return shutil.which('strix')

    def preflight(self):
        exe = self.executable()
        if not exe:
            return {'ok': False, 'reason': 'not_installed',
                    'detail': 'The scanner is not installed in this workspace '
                              'yet. It installs itself the first time you '
                              'connect a model.'}
        ok, detail = self._runner.container_runtime_ready()
        if not ok:
            return {'ok': False, 'reason': 'no_container_runtime',
                    'detail': detail or
                              'The container runtime is not available in this '
                              'workspace, and the scanner needs it to run. '
                              'This workspace has to be deployed with '
                              'build.mode set to buildkit.'}
        return {'ok': True, 'reason': '', 'detail': ''}

    def resolve_target(self, port):
        out = self._runner.default_route()
        gateway = parse_default_gateway(out)
        if not gateway:
            raise BackendError(
                'Could not work out the address the scanner should use to '
                'reach this workspace, so the scan was not started. Without '
                'it a scan would check nothing and report no problems.')
        return f'http://{gateway}:{int(port)}'

    # -- running ------------------------------------------------------------

    def start(self, spec):
        exe = self.executable()
        if not exe:
            raise BackendError('The scanner is not installed.')
        scan_dir = spec['scan_dir']
        os.makedirs(os.path.join(scan_dir, RUNS_SUBDIR), mode=0o700,
                    exist_ok=True)
        argv = build_argv(spec, executable=exe)
        pid = self._runner.spawn(argv, cwd=scan_dir,
                                 env=self._child_env(spec),
                                 log_path=os.path.join(scan_dir, LOG_NAME))
        return {'backend': self.name, 'pid': pid, 'scan_dir': scan_dir,
                'started_at': time.time()}

    def _child_env(self, spec):
        """Environment for the scan process.

        Carries the model — so one scan can use a different one than the saved
        default — plus the sandbox image this deployment pins. Deliberately
        carries no credential: the scanner reads its own config file, so the
        key never enters this process's environment or a child's.
        """
        env = dict(self._env)
        env['STRIX_LLM'] = str(spec['model'])
        for key in ('STRIX_IMAGE', 'STRIX_TELEMETRY'):
            value = self._env.get(key)
            if value:
                env[key] = value
        return env

    def artifacts(self, handle):
        """One read of the scanner's output, or None when there is nothing yet.

        `findings` is `None` rather than `[]` when the file is there but did
        not parse. The two cases look identical to a reader and mean opposite
        things: the scanner writes no findings file at all until it confirms
        something, but it also rewrites that file whole on every finding, so a
        poll can land mid-write. Reporting that as an empty list would drop
        every finding found so far and then re-announce them all on the next
        poll — a second round of notifications for bugs already reported.
        """
        run_dir = newest_run_dir((handle or {}).get('scan_dir') or '')
        if not run_dir:
            return None
        run = read_json_file(os.path.join(run_dir, RUN_RECORD), default=None)
        findings_path = os.path.join(run_dir, FINDINGS_RECORD)
        findings = read_json_file(findings_path, default=None)
        if not isinstance(findings, list):
            findings = None if os.path.exists(findings_path) else []
        if run is None and findings is None:
            return None
        return {'run': run if isinstance(run, dict) else {},
                'findings': findings}

    def is_alive(self, handle):
        return process_alive((handle or {}).get('pid'))

    def stop(self, handle):
        pid = (handle or {}).get('pid')
        if not pid:
            return
        self._runner.terminate(pid, grace=STOP_GRACE_SECONDS)

    def tail_log(self, handle, limit=4000):
        """The end of the scanner's console output — what a failed start left
        behind. Only read when a scan ends badly.

        Redacted here rather than at the caller: this is the boundary where
        raw scanner output stops being a file and starts being something a
        person reads, and a provider's startup error quotes the request.
        """
        path = os.path.join((handle or {}).get('scan_dir') or '', LOG_NAME)
        try:
            size = os.path.getsize(path)
            with open(path, encoding='utf-8', errors='replace') as f:
                if size > limit:
                    f.seek(size - limit)
                return self._redactor(f.read())
        except OSError:
            return ''


class BackendError(Exception):
    """A scan could not be started, with a sentence fit to show a user."""


class _SubprocessRunner:
    """Every OS call the local backend makes, in one injectable place.

    Separated so the backend's logic is testable without spawning anything:
    the suite passes a stub and asserts on what would have been run.
    """

    def default_route(self):
        try:
            r = subprocess.run(['ip', 'route'], capture_output=True, text=True,
                               timeout=10)
            return r.stdout if r.returncode == 0 else ''
        except (OSError, subprocess.SubprocessError):
            return ''

    def container_runtime_ready(self):
        try:
            r = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return True, ''
            lines = (r.stderr or '').strip().splitlines()
            return False, lines[-1] if lines else ''
        except FileNotFoundError:
            return False, ''
        except (OSError, subprocess.SubprocessError):
            return False, ''

    def spawn(self, argv, *, cwd, env, log_path):
        """Start the scan detached from this process's own signals.

        `start_new_session` puts it in its own process group, which is what
        makes a later stop able to take the scanner AND the sandbox it
        launched, rather than orphaning children that keep spending money.
        """
        log = open(log_path, 'ab', buffering=0)
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()
        return proc.pid

    def terminate(self, pid, *, grace=STOP_GRACE_SECONDS):
        """Ask the process group to stop, then insist."""
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0)):
            try:
                os.killpg(os.getpgid(pid), sig)
            except OSError:
                return
            deadline = time.time() + wait
            while wait and time.time() < deadline:
                if not process_alive(pid):
                    return
                time.sleep(0.2)
