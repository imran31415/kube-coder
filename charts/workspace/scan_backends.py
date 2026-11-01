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
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from abc import ABC, abstractmethod


#: Where the scanner leaves its output, relative to the directory it is run in.
#: Its own constant; we run each scan in its own directory so the results land
#: inside that scan's folder rather than in a shared tree.
RUNS_SUBDIR = 'strix_runs'
RUN_RECORD = 'run.json'
FINDINGS_RECORD = 'vulnerabilities.json'

#: The scanner's own end-of-run record, beside its run record. Two things are
#: read from it: `completeness.exit_reason`, the one place the scanner says
#: WHY a run stopped short — its spending limit, a provider that kept refusing
#: requests — which neither its run status nor its exit code carries; and
#: `completeness.complete` + `caveats`, its own verdict on whether the run is
#: a full account of the app. Without that verdict a run that was cut short
#: reads exactly like a clean one.
END_RECORD = 'coverage.json'

#: The scanner's write-up, saved when a run finishes: an executive summary,
#: what it did, what it concluded. It is the result the user paid for, and it
#: is a file on the workspace they can also open themselves — so its path
#: travels beside its text.
REPORT_RECORD = 'penetration_test_report.md'
#: Read with a cap rather than whole: it is shown on a page, and a report
#: longer than this is a runaway rather than something anyone will read.
REPORT_MAX_BYTES = 256 * 1024
#: The scanner's detailed log inside its run folder. Never read here; named in
#: the server log so whoever debugs a failed scan knows where to look.
RUN_LOG = 'strix.log'

#: The scanner's console output, kept for diagnosing a scan that failed to
#: start. Capped because an unbounded log on a PVC is a slow disk-full.
LOG_NAME = 'scanner.log'
LOG_MAX_BYTES = 512 * 1024
#: How much of the end of that output is searched for the error. The scanner
#: prints a final status box on the way out, so the error can sit well above
#: the last few lines.
ERROR_SEARCH_BYTES = 64 * 1024
#: Lines of console output copied into the server log when a scan ends badly.
LOG_EXCERPT_LINES = 30

#: How long a stopped process gets to exit cleanly before it is killed.
STOP_GRACE_SECONDS = 10

#: The scanner's own setting for which container network its sandbox joins
#: (`strix/runtime/docker_client.py` in 1.6.2). Unset — as the chart leaves it —
#: the sandbox joins the runtime's default network, which docker names
#: "bridge". Read here so the address is looked up on the network the sandbox
#: is actually on, not on the one it usually is.
SANDBOX_NETWORK_ENV = 'STRIX_DOCKER_SANDBOX_NETWORK'
DEFAULT_SANDBOX_NETWORK = 'bridge'
#: Docker's own rule for a network name. Anything else is refused before it
#: reaches a command line, where a leading dash would read as a flag.
_NETWORK_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')

#: How long the reachability check waits for the app to accept a connection.
#: The address belongs to this pod, so the answer is immediate or never; the
#: limit only matters if something is badly wrong.
PROBE_TIMEOUT_SECONDS = 3


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

        Discovered at call time, and raises `BackendError` rather than return
        an address it cannot vouch for. A wrong address does not error on its
        own — it produces a clean report against nothing.
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

    def explain_end(self, handle):
        """`(reason, detail, cause)` for a scan that ended without finishing.

        `reason` is for the person who started the scan: one or two plain
        sentences, or '' when this backend cannot tell. `detail` is for
        whoever debugs it later and goes to the server log, never to the UI —
        exit status, where the full output is kept, the end of that output.
        `cause` is what the page picks its next step by: `'provider'` (fixed
        in the model settings, or by waiting), `'budget'` (a higher limit),
        `'scanner'` (anything else the scanner reported), or '' when unknown.

        Not abstract, so a backend that knows nothing still satisfies the
        contract. The caller then says the reason is unknown rather than
        inventing one.
        """
        return '', '', ''


# ── pure helpers (unit-tested without a scanner, a daemon or a network) ─────

def _usable_gateway(value):
    """Is this an IPv4 address a sandbox could reach the workspace on?

    The value goes into a URL, so anything that is not a plain address is
    refused rather than interpolated. Loopback and the unspecified address are
    refused too: from inside the sandbox they mean the sandbox itself.
    """
    if not isinstance(value, str):
        return False
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_unspecified)


def parse_bridge_gateway(inspect_output):
    """The gateway of the sandbox's container network, or None.

    `inspect_output` is what `docker network inspect <network>` prints for the
    network the sandbox joins — the default "bridge" unless the scanner is
    told otherwise (`SANDBOX_NETWORK_ENV`). Because the runtime shares this
    pod's network namespace, the bridge's gateway is this pod — the address
    the workspace's own ports answer on from inside the sandbox. It is
    conventionally 172.17.0.1, but that is a default the runtime is free to
    change, and a wrong guess here is the silent-empty-result failure — so it
    is read, never assumed.

    Not `ip route` in this container, which is what this once did. That reads
    the pod's own routing table, whose default route leads out to the
    Kubernetes node: a scan aimed there attacks the wrong machine and reports
    it clean. (The `ip` tool is not even installed in the workspace image,
    which is how the mistake surfaced.)
    """
    try:
        networks = json.loads(inspect_output or '')
    except ValueError:
        return None
    if isinstance(networks, dict):
        networks = [networks]
    for net in networks if isinstance(networks, list) else []:
        ipam = net.get('IPAM') if isinstance(net, dict) else None
        configs = ipam.get('Config') if isinstance(ipam, dict) else None
        for cfg in configs if isinstance(configs, list) else []:
            gateway = cfg.get('Gateway') if isinstance(cfg, dict) else None
            if _usable_gateway(gateway):
                return gateway
    return None


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


def parse_completeness(document):
    """The scanner's verdict on its own run, or None.

    `{'complete': bool, 'caveats': [sentence, ...]}` — the scanner's own
    words for what it did not get to. Only these two fields: the rest of the
    document is a per-surface breakdown this UI has nowhere to put yet, and
    carrying it would mean storing the whole thing in every scan record.
    """
    completeness = (document or {}).get('completeness')
    if not isinstance(completeness, dict):
        return None
    caveats = completeness.get('caveats')
    return {
        'complete': bool(completeness.get('complete')),
        'caveats': [c for c in caveats if isinstance(c, str) and c.strip()]
                   if isinstance(caveats, list) else [],
    }


def process_alive(pid):
    """Is this pid running? False for a pid we may not signal."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


# ── why a scan ended ───────────────────────────────────────────────────────

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
#: Rounded corners on a UTF-8 console, which is what the workspace gives the
#: scanner; square ones are the fallback its terminal library draws without.
_PANEL_TOP = '╭┏┌'
_PANEL_BOTTOM = '╰┗└'
_PANEL_SIDE = '│┃'
#: The scanner leads every error box with an all-capitals headline ("LLM
#: CONNECTION FAILED", "DOCKER NOT INSTALLED"). Its other boxes have one too
#: ("MODEL QUALITY WARNING"), so only a headline that names a failure counts.
_ERROR_HEADLINE_RE = re.compile(
    r'^[A-Z][A-Z0-9 /_-]{3,}$')
_FAILURE_WORDS_RE = re.compile(
    r'FAILED|NOT AVAILABLE|NOT INSTALLED|UNAVAILABLE|MISSING|UNKNOWN')
_MESSAGE_PREFIXES = ('Error:', 'Details:')
_CRASH_PREFIX = 'Error during penetration test:'
_TRACEBACK_HEAD = 'Traceback (most recent call last):'
_EXCEPTION_LINE_RE = re.compile(
    r'^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Failure)(?::\s?.*)?$')
#: The scanner's own log line format, as it reaches the console at ERROR.
_LOG_ERROR_RE = re.compile(
    r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} (?:ERROR|CRITICAL)\s+\S+ \S+ '
    r'[\w.]+: (.+)$')
#: A provider's error body quoted inside the message — the sentence a person
#: can act on is its `message` field, not the JSON around it.
_PROVIDER_MESSAGE_RE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)+)"')
#: An error that came back from the model provider. Every one reaches the
#: scanner through LiteLLM, whose exceptions print as `litellm.<Name>Error`.
_PROVIDER_ERROR_RE = re.compile(r'\blitellm\.|^LLM connection failed', re.I)
_ERROR_TEXT_MAX = 300


def _unbox(log_text):
    """Console output as `[('box', [paragraph, ...]) | ('line', text)]`.

    The scanner writes errors for a terminal: a box whose text is wrapped to
    the box's width, padded, and closed with a border. Reading that line by
    line is how the error once reached the UI as six lines of border and the
    tail of a provider's JSON, the sentence itself cut off above them. So the
    box is taken apart first and what it wrapped is joined back up, one
    paragraph per blank line inside it.
    """
    blocks = []
    box = None
    paragraph = []

    def end_paragraph():
        if box is not None and paragraph:
            box.append(' '.join(paragraph))
        paragraph.clear()

    for raw in _ANSI_RE.sub('', log_text or '').splitlines():
        line = raw.strip()
        if line[:1] and line[0] in _PANEL_TOP:
            box = []
            blocks.append(('box', box))
            continue
        if box is not None and line[:1] and line[0] in _PANEL_BOTTOM:
            end_paragraph()
            box = None
            continue
        if box is not None and line[:1] and line[0] in _PANEL_SIDE:
            inner = line.strip(_PANEL_SIDE).strip()
            if inner:
                paragraph.append(inner)
            else:
                end_paragraph()
            continue
        end_paragraph()
        box = None
        # Blank lines are kept: they are where a wrapped line ends.
        blocks.append(('line', line))
    end_paragraph()
    return blocks


def _clip(text):
    text = ' '.join((text or '').split())
    if len(text) > _ERROR_TEXT_MAX:
        text = text[:_ERROR_TEXT_MAX - 1].rstrip() + '…'
    return text


def _headline(text):
    """`LLM CONNECTION FAILED` → `LLM connection failed`, for a sentence."""
    first, _, rest = text.partition(' ')
    first = first if len(first) <= 3 else first.capitalize()
    return f'{first} {rest.lower()}'.strip()


def _boxed_error(paragraphs):
    """`headline: message` from one error box, or '' when it is not one."""
    if not paragraphs:
        return ''
    headline = paragraphs[0]
    if not (_ERROR_HEADLINE_RE.match(headline)
            and _FAILURE_WORDS_RE.search(headline)):
        return ''
    title = _headline(headline)
    body = paragraphs[1:]
    for paragraph in body:
        for prefix in _MESSAGE_PREFIXES:
            if paragraph.startswith(prefix):
                return f'{title}: {paragraph[len(prefix):].strip()}'
    return f'{title}: {body[0]}' if body else title


def _following_lines(blocks, start):
    """A console line plus the lines the terminal width wrapped it onto."""
    parts = [blocks[start][1]]
    for kind, text in blocks[start + 1:]:
        if kind != 'line' or not text or text.startswith(_TRACEBACK_HEAD):
            break
        parts.append(text)
    return ' '.join(parts)


def scanner_error(log_text):
    """The scanner's own account of why it stopped, from its console output.

    Returns one line of at most a few hundred characters, or '' when the
    output holds no error at all — a process killed from outside prints
    nothing. Looks for the error in each shape the scanner prints one in,
    and takes the newest: an error box, the one-line "Error during
    penetration test", the last line of a Python traceback, and failing all
    of those, its last logged ERROR.
    """
    blocks = _unbox(log_text)
    found = ''
    in_traceback = False
    logged = ''
    for i, (kind, value) in enumerate(blocks):
        if kind == 'box':
            boxed = _boxed_error(value)
            if boxed:
                found = boxed
            continue
        if value.startswith(_CRASH_PREFIX):
            found = _following_lines(blocks, i)[len(_CRASH_PREFIX):].strip()
            in_traceback = False
        elif value.startswith(_TRACEBACK_HEAD):
            in_traceback = True
        elif in_traceback and _EXCEPTION_LINE_RE.match(value):
            # A traceback's last line names the exception; one printed after
            # the "Error during" line repeats it with its type, which is the
            # more useful of the two to match a hint against.
            found = _following_lines(blocks, i)
            in_traceback = False
        else:
            match = _LOG_ERROR_RE.match(value)
            if match:
                logged = match.group(1)
    return _clip(found or logged)


def is_provider_error(error):
    """Did this error come from the model provider?

    Only those get the provider hint table's advice. Matched against
    anything else it misleads: "The 'docker' CLI was not found" reads to the
    table as a model that was not found.
    """
    return bool(_PROVIDER_ERROR_RE.search(error or ''))


def provider_message(error):
    """The sentence inside a provider's quoted error body, or ''.

    `litellm.ServiceUnavailableError: GeminiException - {"error": {"code":
    503, "message": "This model is currently experiencing high demand…"}}`
    is the whole truth, but only its `message` is something a person reads.
    """
    match = _PROVIDER_MESSAGE_RE.search(error or '')
    if not match:
        return ''
    try:
        return json.loads(f'"{match.group(1)}"')
    except ValueError:
        return match.group(1)


#: Why the scanner says it stopped short, in its end record, in plain words.
#: Keyed on the scanner's own value; one it adds later reads as "unknown"
#: rather than as something it did not say.
SCANNER_END_REASONS = {
    'budget_exceeded': ('The scan reached its spending limit, so the scanner '
                        'stopped.'),
    'rate_limited': ('The model provider kept refusing requests because of its '
                     'rate limit, so the scanner gave up. Free keys hit this '
                     'quickly. Wait a while, or use a key with a higher '
                     'limit.'),
}


def describe_exit(code):
    """A process exit status in plain words, or '' when it says nothing.

    A signal is reported ahead of anything the output says: it is definitive
    about how the process ended, while the newest error in the output may be
    one the scan had already recovered from.
    """
    if not isinstance(code, int) or isinstance(code, bool) or code == 0:
        return ''
    if code < 0:
        if -code == signal.SIGKILL:
            return ('The scanner was killed by the system before it finished. '
                    'In a workspace that usually means it ran out of memory. '
                    'Close other heavy programs, then scan again.')
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f'signal {-code}'
        return f'The scanner was ended by {name} before it finished.'
    return (f'The scanner exited with code {code} without printing a '
            f'reason.')


#: What the scanner writes as its run status when a signal reaches it. It
#: catches the signal, records this, and exits with an ordinary error code,
#: so the exit status alone cannot tell this case from a crash.
SIGNALLED_STATUS = 'interrupted'


def end_reason(*, scanner_reason='', exit_code=None, error='', hint='',
               scanner_status=''):
    """One or two sentences on why a scan ended, from what the backend knows.

    Most specific first: the scanner's own stated reason, then a signal —
    one it died of, or one it caught and recorded — then the error it
    printed (with a plain-English hint in front when one applies), then a
    bare exit code. '' when there is nothing to go on.
    """
    if scanner_reason in SCANNER_END_REASONS:
        return SCANNER_END_REASONS[scanner_reason]
    if isinstance(exit_code, int) and exit_code < 0:
        return describe_exit(exit_code)
    if scanner_status == SIGNALLED_STATUS and not error:
        return ('The scanner was told to stop from outside kube-coder — by '
                'another program, or by someone in a terminal.')
    if error:
        said = provider_message(error) or error
        lead = f'{hint} ' if hint else ''
        return f'{lead}The scanner said: {said}'
    return describe_exit(exit_code)


def end_cause(*, scanner_reason='', exit_code=None, error=''):
    """The `cause` half of `explain_end`, in the same order as `end_reason`.

    Same precedence on purpose, so the page's headline always agrees with the
    sentence under it: a signal outranks an error printed earlier, which the
    scan may have recovered from, and is reported as unknown — a process the
    system killed was not failed by its model.
    """
    if scanner_reason == 'budget_exceeded':
        return 'budget'
    if scanner_reason == 'rate_limited':
        return 'provider'
    if isinstance(exit_code, int) and exit_code < 0:
        return ''
    if error:
        return 'provider' if is_provider_error(error) else 'scanner'
    return ''


# ── the local backend ──────────────────────────────────────────────────────

class LocalStrixBackend(ScanBackend):
    """Runs the scanner as a subprocess in this pod.

    The scanner drives its own container sandbox through the workspace's
    container runtime, so this backend's preflight checks both: the scanner is
    installed, and the runtime answers.
    """

    name = 'local-strix'

    def __init__(self, *, executable=None, env=None, runner=None,
                 redactor=None, explainer=None):
        #: Injected so tests drive a stub and never spawn anything. Production
        #: passes nothing and gets the real subprocess module.
        self._runner = runner or _SubprocessRunner()
        self._executable = executable
        self._env = env if env is not None else os.environ
        #: Strips the user's credential out of scanner output by exact value.
        #: Injected rather than imported: this module never holds a secret, and
        #: giving it one so it could redact would defeat the point.
        self._redactor = redactor or (lambda text: text)
        #: A provider error → one plain sentence on what it means, or ''. The
        #: connection check already keeps that table; injected for the same
        #: reason as the redactor, so the two never drift apart.
        self._explainer = explainer or (lambda text: '')

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
        # Part of readiness, not only of starting: a workspace that can never
        # resolve an address is not ready, and the connection view says so
        # without a scan having to be attempted first.
        _, problem = self._gateway()
        if problem:
            return {'ok': False, 'reason': 'no_target_address',
                    'detail': problem}
        return {'ok': True, 'reason': '', 'detail': ''}

    def resolve_target(self, port):
        gateway, problem = self._gateway()
        if problem:
            raise BackendError(problem)
        port = int(port)
        # The last check before money is spent. The apps list says what is
        # listening and on which address, but only a connection proves the
        # sandbox's address reaches it — and the app may have stopped since
        # the list was read. This pod owns the gateway address, so a
        # connection from here meets the same sockets the sandbox's will.
        if not self._runner.port_answers(gateway, port):
            raise BackendError(
                f'Nothing answered on port {port} at {gateway}, the address '
                f'the scanner reaches this workspace on, so the scan was not '
                f'started. Check the app is still running and listening on '
                f'0.0.0.0, not only on 127.0.0.1.')
        return f'http://{gateway}:{port}'

    def _sandbox_network(self):
        """The container network the scanner's sandbox will join."""
        return ((self._env.get(SANDBOX_NETWORK_ENV) or '').strip()
                or DEFAULT_SANDBOX_NETWORK)

    def _gateway(self):
        """`(address, None)`, or `(None, a sentence saying why not)`.

        The runtime's own error is carried into the sentence. Without it every
        cause — no daemon, no such network, an address we refused — reads the
        same, and the one that shipped first went undiagnosed for that reason.
        """
        problem = ('Could not work out the address the scanner should use to '
                   'reach this workspace, so the scan was not started. Without '
                   'it a scan would check nothing and report no problems.')
        network = self._sandbox_network()
        if not _NETWORK_NAME_RE.match(network):
            return None, (f'{problem} {SANDBOX_NETWORK_ENV} is set to '
                          f'something that is not a network name.')
        out, why = self._runner.network_inspect(network)
        gateway = parse_bridge_gateway(out)
        if gateway:
            return gateway, None
        if why:
            problem += f' The container runtime said: {why}'
        return None, problem

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

        `report` and `coverage` are None until the scanner writes them, which
        it does once, at the end. Both are the scan's actual result: the
        write-up, and the scanner's own statement of what it did not reach.
        A read that returns neither is an ordinary mid-scan read.
        """
        run_dir = newest_run_dir((handle or {}).get('scan_dir') or '')
        if not run_dir:
            return None
        run = read_json_file(os.path.join(run_dir, RUN_RECORD), default=None)
        findings_path = os.path.join(run_dir, FINDINGS_RECORD)
        findings = read_json_file(findings_path, default=None)
        if not isinstance(findings, list):
            findings = None if os.path.exists(findings_path) else []
        report = self._read_report(run_dir)
        coverage = parse_completeness(
            read_json_file(os.path.join(run_dir, END_RECORD), default=None))
        if run is None and findings is None and report is None:
            return None
        return {'run': run if isinstance(run, dict) else {},
                'findings': findings,
                'report': report,
                'coverage': coverage}

    def _read_report(self, run_dir):
        """`{'text', 'location'}` for the scanner's write-up, or None.

        The path travels with the text because the file is on the user's own
        workspace and outlives this page — they can open it, keep it, send it
        on. It is passed up as an opaque label rather than built anywhere
        above the seam: a backend running the scan elsewhere would hand up
        its own location, or none, and nothing above here would change.
        """
        path = os.path.join(run_dir, REPORT_RECORD)
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                text = f.read(REPORT_MAX_BYTES)
        except OSError:
            return None
        text = self._redactor(text).strip()
        if not text:
            return None
        return {'text': text, 'location': path}

    def is_alive(self, handle):
        return self._runner.alive((handle or {}).get('pid'))

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

    def _scanner_end_reason(self, handle):
        """The scanner's own word for why its run stopped short, or ''."""
        run_dir = newest_run_dir((handle or {}).get('scan_dir') or '')
        if not run_dir:
            return ''
        record = read_json_file(os.path.join(run_dir, END_RECORD), default={})
        completeness = (record.get('completeness')
                        if isinstance(record, dict) else None)
        reason = (completeness.get('exit_reason')
                  if isinstance(completeness, dict) else None)
        return reason if isinstance(reason, str) else ''

    def explain_end(self, handle):
        handle = handle or {}
        output = self.tail_log(handle, limit=ERROR_SEARCH_BYTES)
        error = scanner_error(output)
        exit_code = self._runner.exit_status(handle.get('pid'))
        stated = self._scanner_end_reason(handle)
        status = ((self.artifacts(handle) or {}).get('run') or {}).get('status')
        hint = self._explainer(error) if is_provider_error(error) else ''
        reason = end_reason(scanner_reason=stated, exit_code=exit_code,
                            error=error, hint=hint,
                            scanner_status=status or '')
        cause = end_cause(scanner_reason=stated, exit_code=exit_code,
                          error=error)

        scan_dir = handle.get('scan_dir') or ''
        run_dir = newest_run_dir(scan_dir)
        lines = [ln for ln in output.splitlines() if ln.strip()]
        detail = [
            f'exit status: {_exit_text(exit_code)}',
            f'scanner run status: {status or "none recorded"}',
            f'scanner end reason: {stated or "none recorded"}',
            f'scanner error: {error or "none printed"}',
            f'console output: {os.path.join(scan_dir, LOG_NAME)}',
        ]
        if run_dir:
            detail.append(f'run log: {os.path.join(run_dir, RUN_LOG)}')
        if lines:
            detail.append(f'last {min(len(lines), LOG_EXCERPT_LINES)} lines '
                          f'of console output:')
            detail.extend(f'  {ln}' for ln in lines[-LOG_EXCERPT_LINES:])
        return reason, '\n'.join(detail), cause


def _exit_text(code):
    if code is None:
        # A scan this server process did not start — it was restarted while
        # the scan ran — has no exit status to collect.
        return 'unknown'
    if code < 0:
        try:
            return f'{code} ({signal.Signals(-code).name})'
        except ValueError:
            return str(code)
    return str(code)


class BackendError(Exception):
    """A scan could not be started, with a sentence fit to show a user."""


class _SubprocessRunner:
    """Every OS call the local backend makes, in one injectable place.

    Separated so the backend's logic is testable without spawning anything:
    the suite passes a stub and asserts on what would have been run.
    """

    #: pid → the `Popen` of every scan this process started. Class-level
    #: because the backend is built afresh for every call. Holding the object
    #: is what makes the exit status readable at all — it is the one thing
    #: that says a scan was killed by the system rather than failing — and
    #: polling it reaps the child. Without that a finished scan lingers as a
    #: zombie, which `kill(pid, 0)` reports as alive, so the stop below sat
    #: out its whole grace period on a process that had already exited.
    _children = {}
    _children_lock = threading.Lock()

    def alive(self, pid):
        """Is this scan's process still running?"""
        with self._children_lock:
            proc = self._children.get(pid)
        if proc is not None:
            return proc.poll() is None
        # Started by an earlier server process, which a restart left behind:
        # no `Popen` to ask, and not ours to reap.
        return process_alive(pid)

    def exit_status(self, pid):
        """The process's exit status — negative for a signal — or None when
        it is still running or this server process did not start it."""
        with self._children_lock:
            proc = self._children.get(pid)
        return None if proc is None else proc.poll()

    def network_inspect(self, name):
        """`(inspect output, '')`, or `('', why it could not be read)`.

        Asked of the container runtime, which owns the network the sandbox
        joins — never of this container's routing table (see
        `parse_bridge_gateway`). Needs only the docker CLI the scanner
        already requires.
        """
        try:
            r = subprocess.run(['docker', 'network', 'inspect', name],
                               capture_output=True, text=True, timeout=20)
        except FileNotFoundError:
            return '', 'the docker command is not installed.'
        except (OSError, subprocess.SubprocessError) as e:
            return '', str(e)
        if r.returncode == 0:
            return r.stdout, ''
        lines = (r.stderr or '').strip().splitlines()
        return '', lines[-1] if lines else ''

    def port_answers(self, host, port, *, timeout=PROBE_TIMEOUT_SECONDS):
        """Does anything accept a TCP connection on `host:port` right now?

        Opens and closes one connection and sends nothing, so the app sees a
        client that hung up rather than a request.
        """
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

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
        with self._children_lock:
            # Finished scans are dropped here rather than when they end: the
            # exit status has to outlive the process long enough for the
            # poller to read it.
            for old in [p for p, c in self._children.items()
                        if c.returncode is not None]:
                self._children.pop(old, None)
            self._children[proc.pid] = proc
        return proc.pid

    def terminate(self, pid, *, grace=STOP_GRACE_SECONDS):
        """Ask the process group to stop, then insist.

        The group id is the pid itself — `spawn` starts every scan as its own
        session leader — so the group is still reachable once the leader has
        been reaped, which `getpgid(pid)` would no longer be.
        """
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0)):
            try:
                os.killpg(pid, sig)
            except OSError:
                return
            deadline = time.time() + wait
            while wait and time.time() < deadline:
                if not self.alive(pid):
                    return
                time.sleep(0.2)
