"""Guard: test fixtures must not do filesystem I/O at deployment paths.

WHY THIS EXISTS. The development workspace *is* the deployment environment —
`/home/dev` is a real PVC mount here, `/opt/browser-src` is a real image path.
So a test that writes its fixtures to one of those passes locally, passes
under `kc-preflight` (which runs this same suite in this same pod), and then
fails in CI, where the runner is a stock `ubuntu-latest` with none of those
paths.

That happened in #559: an API test built its fixture tree with
`tempfile.mkdtemp(dir='/home/dev')`, went green locally and through preflight,
and errored every test in its suite on CI. Local-green-CI-red is the most
expensive failure shape we have — it costs a full push/wait/read cycle to
discover something a lint could have caught in a second — and no amount of
running the suite *here* will ever catch it.

WHAT IS AND IS NOT FLAGGED. Referring to a deployment path as a *string* is
completely legitimate and common — asserting a default workdir is `/home/dev`,
checking an error message mentions it, passing it as a value under test. There
are ~115 such uses and none of them are a problem.

What breaks CI is *touching the filesystem* at those paths. So this walks the
AST and flags only calls that perform real I/O — `open`, `os.makedirs`,
`shutil.rmtree`, `tempfile.mkdtemp(dir=...)` and friends — whose target is a
deployment-path literal. Fixtures belong in `tempfile.mkdtemp()` with no
`dir=`, and code under test should take its root from a patchable constant
(see `server.INSTRUCTION_SCAN_ROOT`, which exists precisely so its tests do
not need `/home/dev`).

Run with:    python3 -m unittest tests.no_deployment_paths_test
(from charts/workspace/)
"""

from __future__ import annotations

import ast
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

# Paths that exist only inside a provisioned workspace pod or its image.
# A CI runner has none of them.
DEPLOYMENT_PREFIXES = (
    '/home/dev',
    '/home/ubuntu',
    '/opt/browser-src',
    '/tmp/browser',
    '/claude-config',
    '/github-app',
    '/certs',
)

# Test trees to police. Both charts ship their own suite.
TEST_DIRS = (
    os.path.join(REPO, 'charts', 'workspace', 'tests'),
    os.path.join(REPO, 'charts', 'workspace-controller', 'tests'),
)

# Calls that actually touch the filesystem. Matched on the trailing attribute
# name, so `os.makedirs`, `shutil.rmtree` and a bare `makedirs` all hit.
FS_CALLS = frozenset({
    'open', 'makedirs', 'mkdir', 'remove', 'unlink', 'rmdir', 'rmtree',
    'copy', 'copy2', 'copyfile', 'copytree', 'move', 'symlink', 'link',
    'chmod', 'chown', 'touch', 'write_text', 'write_bytes', 'listdir',
    'walk', 'scandir', 'glob',
})

# Calls where a deployment path arrives via `dir=` rather than positionally.
# This is the exact shape that broke #559.
DIR_KWARG_CALLS = frozenset({'mkdtemp', 'mkstemp', 'TemporaryDirectory',
                             'NamedTemporaryFile'})


def _is_deployment_literal(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and any(node.value == p or node.value.startswith(p + '/')
                    for p in DEPLOYMENT_PREFIXES))


def _callee_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ''


def _violations_in(path: str):
    """Yield (lineno, callee, literal) for filesystem I/O at a deployment path."""
    with open(path, 'r', encoding='utf-8') as fh:
        src = fh.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:            # a syntax error is another test's problem
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name in FS_CALLS:
            for arg in node.args:
                if _is_deployment_literal(arg):
                    yield node.lineno, name, arg.value
        if name in DIR_KWARG_CALLS:
            for kw in node.keywords:
                if kw.arg == 'dir' and _is_deployment_literal(kw.value):
                    yield node.lineno, '{}(dir=)'.format(name), kw.value.value


class NoDeploymentPathsInTests(unittest.TestCase):
    def test_no_test_writes_to_a_deployment_path(self):
        offenders = []
        for tdir in TEST_DIRS:
            if not os.path.isdir(tdir):
                continue
            for name in sorted(os.listdir(tdir)):
                if not name.endswith('.py'):
                    continue
                p = os.path.join(tdir, name)
                for lineno, callee, literal in _violations_in(p):
                    offenders.append('{}:{}  {} -> {!r}'.format(
                        os.path.relpath(p, REPO), lineno, callee, literal))
        self.assertEqual(offenders, [], (
            '\n\nTest fixtures must not touch deployment paths — these pass in '
            'the workspace pod (and under kc-preflight, which runs here) but '
            'fail on a CI runner that has no such directory:\n\n  '
            + '\n  '.join(offenders)
            + '\n\nUse tempfile.mkdtemp() with no dir=, and give the code under '
              'test a patchable root constant instead of a hardcoded path.\n'))

    def test_scanned_something(self):
        """A guard that silently scans nothing is worse than no guard."""
        count = sum(len([f for f in os.listdir(d) if f.endswith('.py')])
                    for d in TEST_DIRS if os.path.isdir(d))
        self.assertGreater(count, 10, 'expected to find the test suites')


class DetectorUnitTests(unittest.TestCase):
    """The detector itself, so it cannot rot into a no-op."""

    def _find(self, src):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'x.py')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(src)
        return list(_violations_in(p))

    def test_flags_mkdtemp_dir_kwarg(self):
        """The exact shape that broke CI in #559."""
        v = self._find("import tempfile\ntempfile.mkdtemp(dir='/home/dev')\n")
        self.assertEqual(len(v), 1)
        self.assertIn('mkdtemp', v[0][1])

    def test_flags_open_and_makedirs(self):
        self.assertEqual(len(self._find("open('/home/dev/x', 'w')\n")), 1)
        self.assertEqual(len(self._find("import os\nos.makedirs('/opt/browser-src/a')\n")), 1)

    def test_flags_nested_deployment_path(self):
        self.assertEqual(len(self._find("open('/home/ubuntu/.config/x')\n")), 1)

    def test_allows_string_comparisons(self):
        """~115 legitimate uses look like this — none may be flagged."""
        self.assertEqual(self._find(
            "self.assertEqual(cfg['workdir'], '/home/dev')\n"), [])
        self.assertEqual(self._find("ctx = {'workdir': '/home/dev'}\n"), [])
        self.assertEqual(self._find("self.assertIn('/home/dev', err)\n"), [])

    def test_allows_tempdir_without_dir_kwarg(self):
        self.assertEqual(self._find("import tempfile\ntempfile.mkdtemp()\n"), [])

    def test_does_not_flag_a_lookalike_prefix(self):
        """/home/developer is not /home/dev."""
        self.assertEqual(self._find("open('/home/developer/x', 'w')\n"), [])


if __name__ == '__main__':
    unittest.main()
