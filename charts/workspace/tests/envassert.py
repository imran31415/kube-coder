"""Assertions about subprocess environments that never render their values.

WHY THIS EXISTS (#562). Several tests assert that a built subprocess env does
*not* carry some variable:

    self.assertNotIn('ANTHROPIC_API_KEY', spec['env'])

`unittest` renders the whole container in the failure message, and these envs
are copied from `os.environ` (hypervisor_session.py:529) because the real CLI
subprocess needs the real environment. So in a workspace pod that container
holds every secret the `ide` container has. When one of these assertions
failed, the output carried a full RSA private key, a live `GH_TOKEN`, and the
provider keys — into a terminal, and in CI into a workflow log readable by
anyone with repo read access.

The trigger was benign (an agent session exporting `KC_PROJECT_ID`), but the
trigger is not the point: *any* failure of these assertions produced the dump.

So: assert on keys, and render only key *names* on failure. Keys are safe —
`GITHUB_APP_PRIVATE_KEY` is a useful thing to see in a failure message, its
1675-character value is not.

Import from a test module with:

    sys.path.insert(0, HERE)          # HERE = this tests/ directory
    from envassert import assert_env_lacks

which works under both `python3 -m unittest discover -s tests` (the Makefile)
and `python3 -m unittest tests.<name>` (the per-file docstrings).
"""

from __future__ import annotations

from typing import Iterable, Mapping


def _render_keys(env: Mapping[str, str], limit: int = 40) -> str:
    keys = sorted(env)
    shown = keys[:limit]
    tail = '' if len(keys) <= limit else ' … (+{} more)'.format(len(keys) - limit)
    return '[{}]{}'.format(', '.join(shown), tail)


def assert_env_lacks(tc, env: Mapping[str, str], key: str, msg: str = '') -> None:
    """Fail if `key` is present in `env`, without rendering any value.

    Use instead of `assertNotIn(key, env)` whenever `env` is a real or
    os.environ-derived subprocess environment.
    """
    if key in env:
        tc.fail(
            '{}{!r} unexpectedly present in the built environment.\n'
            'This env is copied from os.environ, so a value exported by your '
            'own shell trips it — isolate os.environ in the test rather than '
            'loosening the assertion.\n'
            'env keys: {}'.format((msg + '\n') if msg else '', key,
                                  _render_keys(env)))


def assert_env_has(tc, env: Mapping[str, str], key: str, value: str = None) -> None:
    """Assert presence (and optionally an exact value) without dumping the env.

    Only pass `value` for non-secret variables — it is rendered on failure.
    """
    if key not in env:
        tc.fail('{!r} missing from the built environment\nenv keys: {}'.format(
            key, _render_keys(env)))
    if value is not None and env[key] != value:
        tc.fail('{!r} = {!r}, expected {!r}'.format(key, env[key], value))


def assert_env_lacks_all(tc, env: Mapping[str, str], keys: Iterable[str]) -> None:
    present = [k for k in keys if k in env]
    if present:
        tc.fail('unexpectedly present in the built environment: {}\n'
                'env keys: {}'.format(sorted(present), _render_keys(env)))
