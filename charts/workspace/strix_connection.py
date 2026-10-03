"""Connecting the scanner to a model, and installing it on first use (#726).

Separate from `scan_backends.py` on purpose: that module runs scans and must
never handle a credential, this one handles the credential and never runs a
scan. The split is what lets the backend's tests assert that no key can reach
a command line or a child process's environment.

## The scanner owns its own credentials

kube-coder does not store the user's key. It writes the scanner's own config
file — the same file the `strix` command in the workspace terminal reads — and
the scanner reads it itself when a scan starts. Three consequences, all of them
wanted:

* `ProviderKeysManager.ALLOWED` is untouched. The workspace assistant's keys and
  the scanner's key stay separate stores, so a long scan cannot exhaust the key
  the chat assistant depends on.
* Setting it up in the terminal and setting it up in the dashboard are the same
  action, and either surface sees the other's work.
* No endpoint here ever returns the key. `connection_view()` reports whether one
  is present, never what it is.

## Installed on first use, not baked into the image

The scanner is a large dependency tree that most workspaces will never use, so
it is installed into its own virtual environment the first time somebody
connects a model. Its own environment rather than the system one because it
pins versions of libraries the workspace also uses, and `--break-system-packages`
into a shared site-packages is how two tools end up breaking each other.

The version is pinned by the deployment (`STRIX_VERSION`), so what gets
installed is decided by whoever runs the cluster, not by whenever a user first
pressed the button.
"""

import json
import os
import re
import shlex
import subprocess
import threading
import time


#: Paths are functions rather than import-time constants, for the same reason
#: `ScansManager.scans_dir()` is: a constant computed from HOME at import time
#: can only be redirected by reloading the module, and a reload hands back a
#: DIFFERENT module object than the one the handlers already imported — so the
#: test redirects one copy while the server keeps writing to the other. Asking
#: for the path each time removes the whole class of problem, and costs a
#: string join.
def strix_home():
    return os.environ.get('KC_STRIX_HOME') or os.path.expanduser('~')


def config_dir():
    """The scanner's own settings directory — shared with its CLI."""
    return os.path.join(strix_home(), '.strix')


def config_path():
    """The scanner's config file, in its own `{"env": {...}}` format."""
    return os.path.join(config_dir(), 'cli-config.json')


def venv_dir():
    """Where the scanner is installed. Kept outside the settings directory so
    disconnecting a model cannot disturb the install, or the reverse."""
    return os.path.join(strix_home(), '.strix-venv')


def install_state_path():
    return os.path.join(venv_dir(), '.kc-install.json')

#: The distribution and the environment variable names are the scanner's, not
#: ours — they are its published interface.
PACKAGE = 'strix-agent'
DEFAULT_VERSION = '1.6.2'
MODEL_VAR = 'STRIX_LLM'
KEY_VAR = 'LLM_API_KEY'
BASE_VAR = 'LLM_API_BASE'

#: Install states, as the UI shows them.
INSTALL_STATES = ('absent', 'installing', 'ready', 'failed')

_INSTALL_TIMEOUT = 900
_TEST_TIMEOUT = 90
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
_lock = threading.Lock()


def pinned_version():
    return os.environ.get('STRIX_VERSION') or DEFAULT_VERSION


# ── the scanner's config file ──────────────────────────────────────────────

def read_config():
    """The scanner's saved settings, or an empty config. Never raises."""
    try:
        with open(config_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_config(data):
    """Replace the config file atomically, readable only by its owner.

    A partial write would leave the scanner with half a configuration, and a
    world-readable file would put the user's key where every process in the
    workspace can read it.
    """
    path = config_path()
    os.makedirs(config_dir(), mode=0o700, exist_ok=True)
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_connection(*, model=None, api_key=None, api_base=None):
    """Update the scanner's settings, leaving everything else in the file.

    A merge rather than a replace because the user may have set other options
    from the terminal, and a dashboard save must not silently discard them.
    Passing an empty string for a field clears it; passing None leaves it as
    it was, so the UI can save a new model without re-sending the key.

    Returns `(view, error)`.
    """
    if model is not None:
        model = model.strip()
        if model and (len(model) > 200 or _CONTROL_RE.search(model)):
            return None, 'That model name is not valid.'
    if api_base is not None:
        api_base = api_base.strip()
        if api_base and (len(api_base) > 500 or _CONTROL_RE.search(api_base)):
            return None, 'That server address is not valid.'
    if api_key is not None:
        api_key = api_key.strip()
        if len(api_key) > 4000 or _CONTROL_RE.search(api_key):
            return None, 'That API key is not valid.'

    with _lock:
        data = read_config()
        env = data.get('env')
        if not isinstance(env, dict):
            env = {}
        for var, value in ((MODEL_VAR, model), (KEY_VAR, api_key),
                           (BASE_VAR, api_base)):
            if value is None:
                continue
            if value == '':
                env.pop(var, None)
            else:
                env[var] = value
        data['env'] = env
        _write_config(data)
    return connection_view(), None


def clear_connection():
    """Forget the saved model and key, leaving any other settings alone."""
    with _lock:
        data = read_config()
        env = data.get('env')
        if isinstance(env, dict):
            for var in (MODEL_VAR, KEY_VAR, BASE_VAR):
                env.pop(var, None)
            data['env'] = env
            _write_config(data)
    return connection_view()


def saved_env():
    """The scanner's saved variables. Internal — includes the key."""
    env = read_config().get('env')
    return env if isinstance(env, dict) else {}


#: Below this length a "key" would match ordinary words and blank most of the
#: message, which hides the error the user needs in order to fix anything.
_MIN_REDACTABLE = 8


def redact_known_secrets(text):
    """Remove the saved credential from text, matching its exact value.

    `scans.redact_secrets` blanks credential-SHAPED substrings, which is a
    heuristic: a provider that invents its own key format walks straight
    through it. This module holds the actual value, so here the removal is a
    guarantee rather than a guess. Both run, from opposite directions, because
    provider error messages are the one place a key reliably comes back — they
    quote the request that failed.
    """
    text = text or ''
    if not text:
        return text
    value = (saved_env().get(KEY_VAR) or '').strip()
    if len(value) >= _MIN_REDACTABLE:
        text = text.replace(value, '[redacted]')
    return text


def connection_view():
    """What the API may return: everything except the key itself."""
    env = saved_env()
    key = env.get(KEY_VAR) or ''
    model = env.get(MODEL_VAR) or ''
    subscription = model.startswith('chatgpt/')
    return {
        'model': model,
        'api_base': env.get(BASE_VAR) or '',
        'has_key': bool(key),
        # A subscription model needs no key, so "configured" is not simply
        # "has a key" — saying otherwise would tell a signed-in user they are
        # not connected.
        'uses_subscription': subscription,
        'subscription': subscription_view(),
        'configured': bool(model) and (bool(key) or subscription),
    }


# ── installing on first use ────────────────────────────────────────────────

def _read_install_state():
    try:
        with open(install_state_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_install_state(state, *, error=''):
    path = install_state_path()
    os.makedirs(venv_dir(), mode=0o700, exist_ok=True)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'state': state, 'version': pinned_version(),
                       'error': error, 'updated_at': time.time()}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def executable_path():
    """Where the scanner lands once installed."""
    return os.path.join(venv_dir(), 'bin', 'strix')


def is_installed():
    return os.path.exists(executable_path())


def install_state():
    """`{state, version, error}` — what the UI shows while it waits.

    The recorded state is trusted only when the file system agrees with it: a
    pod that restarted mid-install leaves `installing` behind forever, and a
    spinner that never resolves is worse than an honest "not installed".
    """
    saved = _read_install_state()
    state = saved.get('state')
    if is_installed() and state != 'installing':
        return {'state': 'ready', 'version': saved.get('version') or '',
                'error': ''}
    if state == 'installing':
        with _lock:
            running = _install_thread_alive()
        if not running and not is_installed():
            return {'state': 'failed', 'version': saved.get('version') or '',
                    'error': 'The install did not finish. Try again.'}
        if running:
            return {'state': 'installing',
                    'version': saved.get('version') or '', 'error': ''}
    if state in ('failed', 'ready'):
        return {'state': 'ready' if is_installed() else 'failed',
                'version': saved.get('version') or '',
                'error': saved.get('error') or ''}
    return {'state': 'absent', 'version': '', 'error': ''}


_install_thread = None


def _install_thread_alive():
    return _install_thread is not None and _install_thread.is_alive()


def ensure_installed(runner=None):
    """Start installing the scanner if it is not here yet. Returns the state.

    Returns immediately: installing pulls a large dependency tree, and an HTTP
    request must not sit on it. The UI polls `install_state()`.
    """
    global _install_thread
    if is_installed():
        return install_state()
    # Start the thread while holding the lock, but read the state back outside
    # it. install_state() acquires `_lock` itself, so returning it from inside
    # this block deadlocks the moment a second Save arrives mid-install -- and
    # because the thread dies holding the lock, every later save_connection,
    # clear_connection and GET /api/scans/connection hangs too, until the pod
    # restarts. See tests/strix_connection_test.py for the two-call case.
    with _lock:
        if not _install_thread_alive():
            _write_install_state('installing')
            _install_thread = threading.Thread(
                target=_install, args=(runner or _Installer(), venv_dir()),
                name='scanner-install', daemon=True)
            _install_thread.start()
    return install_state()


def _install(runner, directory=None):
    try:
        ok, detail = runner.install(directory or venv_dir(), PACKAGE,
                                    pinned_version())
    except Exception as e:                    # pragma: no cover - defensive
        ok, detail = False, str(e)
    _write_install_state('ready' if ok else 'failed',
                         error='' if ok else _short(detail))


def _short(text, limit=400):
    """The last few lines of a tool's output — what actually says why."""
    lines = [ln for ln in (text or '').strip().splitlines() if ln.strip()]
    return '\n'.join(lines[-6:])[:limit]


class _Installer:
    """The install commands, isolated so tests never run one."""

    def install(self, venv_dir, package, version):
        steps = (
            [_system_python(), '-m', 'venv', venv_dir],
            [os.path.join(venv_dir, 'bin', 'pip'), 'install', '--no-input',
             '--disable-pip-version-check', f'{package}=={version}'],
        )
        for argv in steps:
            try:
                r = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=_INSTALL_TIMEOUT)
            except FileNotFoundError:
                return False, (f'{argv[0]} is not available in this '
                               f'workspace.')
            except subprocess.SubprocessError as e:
                return False, str(e)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout or
                               f'{argv[0]} exited {r.returncode}')
        return True, ''


def _system_python():
    return os.environ.get('KC_PYTHON') or 'python3'


# ── checking the connection actually works ─────────────────────────────────

#: One token, one round trip. Enough to prove the credential and the model name
#: are both right, cheap enough to run whenever the user presses the button.
_PROBE = (
    'import os, sys, litellm\n'
    'kw = {}\n'
    'base = os.environ.get("KC_PROBE_BASE")\n'
    'if base: kw["api_base"] = base\n'
    'key = os.environ.get("KC_PROBE_KEY")\n'
    'if key: kw["api_key"] = key\n'
    'try:\n'
    '    litellm.completion(model=os.environ["KC_PROBE_MODEL"],\n'
    '                       messages=[{"role": "user", "content": "hi"}],\n'
    '                       max_tokens=1, **kw)\n'
    'except Exception as e:\n'
    '    sys.stderr.write(type(e).__name__ + ": " + str(e)[:600])\n'
    '    sys.exit(1)\n'
)


def test_connection(runner=None):
    """Check the saved settings against the provider. Returns `(ok, detail)`.

    The scanner has no validate-only mode, so this asks the same library the
    scanner uses, in the scanner's own environment, with the same model string
    — which is what makes the answer meaningful rather than a guess.

    The credential is passed to the probe through its environment, never on a
    command line: an argument list is readable by anything that can see the
    process table.
    """
    runner = runner or _Probe()
    if not is_installed():
        return False, 'The scanner is still being installed.'
    env = saved_env()
    model = env.get(MODEL_VAR) or ''
    if not model:
        return False, 'No model is set yet.'
    if model.startswith('chatgpt/'):
        return runner.subscription_status(executable_path())
    if not env.get(KEY_VAR):
        return False, 'No API key is set for this model.'
    ok, detail = runner.probe_model(
        os.path.join(venv_dir(), 'bin', 'python'), model,
        env.get(KEY_VAR), env.get(BASE_VAR) or '')
    # The detail is a provider's own error text, which quotes the request that
    # failed and so is the likeliest place for the key to come back at us.
    return ok, redact_known_secrets(detail)


class _Probe:
    """The two real checks, isolated so tests never make a network call."""

    def subscription_status(self, executable):
        try:
            r = subprocess.run([executable, 'auth', 'status'],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        if r.returncode == 0:
            return True, 'Signed in.'
        return False, 'Not signed in to a subscription yet.'

    def probe_model(self, python, model, key, base):
        env = dict(os.environ)
        env['KC_PROBE_MODEL'] = model
        env['KC_PROBE_KEY'] = key or ''
        env['KC_PROBE_BASE'] = base or ''
        try:
            r = subprocess.run([python, '-c', _PROBE], capture_output=True,
                               text=True, timeout=_TEST_TIMEOUT, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        if r.returncode == 0:
            return True, 'The model answered.'
        return False, explain_provider_error(r.stderr or r.stdout)


#: Where the scanner keeps its own subscription credentials. We only ever ask
#: whether the file is there — its contents are the scanner's business, and a
#: token is not something this module reads, copies or reports.
def subscription_auth_path():
    return os.path.join(config_dir(), 'subscription-auth.json')


def subscription_view():
    """Whether a subscription sign-in has been completed, and nothing more.

    Deliberately derived from the file's presence rather than its contents.
    `connection_view` reports this so the UI can tell "signed in, no key
    needed" from "no credentials at all", which are otherwise identical.
    """
    return {'signed_in': os.path.isfile(subscription_auth_path())}


def sign_out_subscription():
    """Ask the scanner to forget its subscription credentials.

    Shelled out to the scanner's own subcommand rather than removing the file
    ourselves, the same rule `SubscriptionStatusManager` follows: the tool that
    wrote the credential is the one that knows how to revoke it cleanly.
    """
    if not is_installed():
        return False
    try:
        r = subprocess.run([executable_path(), 'auth', 'logout'],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class SubscriptionSignIn:
    """Sign the scanner in to a ChatGPT subscription from the dashboard.

    Mirrors `server.ClaudeWebLoginManager`, because the constraint is the same
    one. The scanner's sign-in normally waits on a callback at a loopback port
    *on the machine running it* — which is this pod, while the browser is on
    the user's laptop, so that callback can never arrive. The scanner provides
    `--manual` for exactly this case: it prints the sign-in URL and then waits
    for the user to paste back the address their browser was redirected to.

    So this drives the scanner's own command in a detached terminal session,
    reads the sign-in URL off the screen for the dashboard to open in the
    user's browser, and passes the pasted redirect back to the waiting prompt.

    **The scanner stays the only thing that handles the credential.** Nothing
    here parses, stores, logs or echoes what the user pastes: it goes straight
    to the waiting process, which exchanges it and writes its own credential
    file. A failure reports the scanner's own message, never the pasted value.
    """

    SESSION = 'kc-scanner-signin'

    #: The sign-in URL the scanner prints. Bounded charset so noise elsewhere
    #: on the screen cannot extend the match.
    _URL_RE = re.compile(
        r'(https://auth\.openai\.com/oauth/authorize[^\s\'"<>]*)')
    _PROMPT_RE = re.compile(r'Paste the full redirect URL', re.IGNORECASE)
    _FAILED_RE = re.compile(r'SIGN-?IN FAILED\s*:?\s*(.*)', re.IGNORECASE)
    _SUCCESS_RE = re.compile(r'Signed in with your ChatGPT subscription',
                             re.IGNORECASE)
    _EXIT_RE = re.compile(r'__KC_SCAN_SIGNIN_EXIT__:(\d+)')
    #: What the scanner asks for: a redirect URL, or `code#state`. Checked for
    #: shape only, as the gate before it is forwarded — the scanner is what
    #: validates it for real.
    _PASTE_RE = re.compile(r'^[A-Za-z0-9_.:/?=&%#~+-]{8,2048}$')

    @staticmethod
    def _tmux(*args):
        return subprocess.run(['tmux', *args], capture_output=True, text=True)

    @classmethod
    def running(cls):
        return cls._tmux('has-session', '-t', cls.SESSION).returncode == 0

    @classmethod
    def cancel(cls):
        """Tear down the sign-in session. Idempotent."""
        cls._tmux('kill-session', '-t', cls.SESSION)

    @classmethod
    def _screen(cls):
        # -J rejoins wrapped lines so the (long) sign-in URL comes back whole.
        r = cls._tmux('capture-pane', '-p', '-J', '-t', cls.SESSION)
        return r.stdout if r.returncode == 0 else ''

    @classmethod
    def parse_url(cls, screen):
        """The sign-in URL once the scanner has printed it, else None.
        Pure text, so it unit-tests without the scanner installed."""
        m = cls._URL_RE.search(screen or '')
        return m.group(1) if m else None

    @classmethod
    def classify(cls, screen):
        """`(state, error)` from the session's screen.

        States: `pending` | `awaiting_paste` | `success` | `failed`. Anchored
        on the exit sentinel first, so a finished command is never reported as
        still waiting.
        """
        screen = screen or ''
        m = cls._EXIT_RE.search(screen)
        if m:
            if m.group(1) == '0' or cls._SUCCESS_RE.search(screen):
                return 'success', None
            failed = cls._FAILED_RE.search(screen)
            detail = (failed.group(1).strip() if failed and failed.group(1)
                      else '')
            return 'failed', detail or 'The sign-in did not complete.'
        if cls._SUCCESS_RE.search(screen):
            return 'success', None
        if cls._PROMPT_RE.search(screen):
            return 'awaiting_paste', None
        return 'pending', None

    @classmethod
    def start(cls, timeout=40):
        """Begin a sign-in and return the URL for the user's own browser.

        Raises RuntimeError with a sentence fit to show when the scanner is
        missing, the session cannot start, or no URL appears in time.
        """
        cls.cancel()
        if not is_installed():
            raise RuntimeError('The scanner is not installed yet.')
        inner = (
            f'{shlex.quote(executable_path())} auth login chatgpt --manual; '
            "printf '__KC_SCAN_SIGNIN_EXIT__:%s\\n' \"$?\"; sleep 600"
        )
        # A wide screen keeps the URL from being hard-truncated; -J on capture
        # handles any soft wrapping that remains.
        started = cls._tmux('new-session', '-d', '-x', '400', '-y', '50',
                            '-s', cls.SESSION, 'bash', '-lc', inner)
        if started.returncode != 0:
            raise RuntimeError(
                (started.stderr or 'Could not start the sign-in.').strip())
        deadline = time.time() + timeout
        while time.time() < deadline:
            screen = cls._screen()
            url = cls.parse_url(screen)
            if url:
                return {'url': url, 'in_progress': True}
            state, err = cls.classify(screen)
            if state == 'failed':
                cls.cancel()
                raise RuntimeError(err or 'The sign-in could not start.')
            time.sleep(0.5)
        cls.cancel()
        raise RuntimeError(
            'Timed out waiting for the sign-in link. Check this workspace\'s '
            'network access and try again.')

    @classmethod
    def submit(cls, pasted):
        """Hand the pasted redirect to the waiting prompt. `(ok, error)`.

        Never logged and never stored: it is a single-use secret, and this
        method's only job is to move it from the request to the process that
        is waiting for it.
        """
        pasted = (pasted or '').strip()
        if not cls._PASTE_RE.match(pasted):
            return False, ('That does not look like the address your browser '
                           'was redirected to.')
        if not cls.running():
            return False, 'No sign-in is in progress. Start again.'
        state, _ = cls.classify(cls._screen())
        if state != 'awaiting_paste':
            return False, 'The sign-in is not waiting for that yet.'
        # -l sends the text literally, with no key-name interpretation.
        sent = cls._tmux('send-keys', '-t', cls.SESSION, '-l', pasted)
        if sent.returncode != 0:
            return False, 'Could not reach the sign-in session. Start again.'
        cls._tmux('send-keys', '-t', cls.SESSION, 'Enter')
        return True, None

    @classmethod
    def poll(cls):
        """Progress. On success the session is cleaned up and the refreshed
        connection view comes back, so the UI updates in one round trip."""
        running = cls.running()
        state, err = cls.classify(cls._screen() if running else '')
        if state == 'success':
            cls.cancel()
            return {'signed_in': True, 'in_progress': False,
                    'connection': connection_view()}
        if state == 'failed' or not running:
            cls.cancel()
            return {'signed_in': False, 'in_progress': False,
                    'error': err or 'The sign-in did not complete.'}
        return {'signed_in': False, 'in_progress': True, 'state': state}


#: Provider errors are written for developers. These are the four a user
#: actually hits, each turned into the sentence that says what to do about it.
#:
#: ORDER IS LOAD-BEARING, and the reason is a real bug this table had: a
#: provider reports a temporary overload as "503 UNAVAILABLE", and a wrong
#: model name as "this model is unavailable". Matching the word `unavailable`
#: first told users to go and change a model that was perfectly correct. So the
#: transient rule is checked first and matches only on unambiguous transient
#: signals (a status code, "overloaded", a rate limit), and `unavailable` is
#: left to the model rule below it.
_ERROR_HINTS = (
    # Quota before "busy", because an exhausted allowance also arrives as
    # a 429 and the two need opposite advice: waiting clears a burst, but a
    # daily cap only resets tomorrow and a spent plan never does. The busy
    # rule below matches every 429, so it would otherwise swallow this.
    (re.compile(r'limit exceeded|quota|insufficient[_ ]quota|billing'
                r'|\bcredit\b|resource[_ ]exhausted', re.I),
     'This key has no quota left with the provider. Free keys have a small '
     'daily allowance; check your plan or billing details, or use a '
     'different key.'),
    (re.compile(r'\b(503|429)\b|overloaded|rate.?limit|too many requests'
                r'|timed? ?out|temporarily', re.I),
     'The provider is busy right now. Wait a moment and try again.'),
    (re.compile(r'unauthor|forbidden|invalid.*key|authentication'
                r'|\b(401|403)\b', re.I),
     'The provider did not accept this key. Check that you copied all of it.'),
    (re.compile(r'not found|unavailable|does not exist|unknown model'
                r'|\b404\b', re.I),
     'That model name is not available on this provider. Check the spelling, '
     'or pick another model.'),
)


def explain_provider_error(raw):
    """A provider's error, plus one plain sentence about what it means.

    Kept as data rather than nested ifs so a maintainer can add a case in one
    line. The raw text is preserved after the hint — the user's provider knows
    things we do not, and hiding its message would make a novel failure
    undiagnosable.
    """
    text = (raw or '').strip()
    if not text:
        return 'The connection test failed, and gave no reason.'
    for pattern, hint in _ERROR_HINTS:
        if pattern.search(text):
            return f'{hint}\n\n{text[:400]}'
    return text[:600]
