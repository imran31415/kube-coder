"""Guard: every server-side launch decides `auto_approve` out loud (#769).

WHY THIS EXISTS. `ClaudeTaskManager.create_task(auto_approve=False)` defaults
to "keep the CLI's permission prompts". That is right for the Build tab, where
a person watches the terminal, and wrong for every launch nobody watches. #296
built `resolve_auto_approve` for exactly that, then wired it into one entry
point. The cron and webhook fire handlers called `create_task` without the
kwarg, inherited the False, and for months launched plain `claude` into the
account's default permission mode, where an unanswerable prompt or dialog
stalls the Build forever (#762, #769). Each call looked fine on its own: an
omitted kwarg has no line to review.

So this walks the AST of every non-test module and requires each
`ClaudeTaskManager.create_task(...)` call to pass `auto_approve=` explicitly,
whether it asks `resolve_auto_approve` or passes a literal on purpose (the
page-watch fire does). A new call site then has to make the decision in the
diff, where a reviewer can see it.

Run with:    python3 -m unittest tests.create_task_contract_test
(from charts/workspace/)
"""

from __future__ import annotations

import ast
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)    # charts/workspace

SKIP_DIRS = frozenset({'tests', 'web', 'node_modules', '__pycache__'})

# (module, enclosing function) -> why it may still omit the kwarg. Every entry
# is a known gap with a reason, not a convenience; the stale-entry test below
# fails as soon as one is fixed or disappears.
ALLOWED_TO_OMIT = {
    ('handlers/desktop.py', 'handle_desktop_launch'):
        "desktop: is listed as unattended, but the default 'New build' icon "
        "opens an empty session a person types into. Reported on #769 as a "
        "follow-up rather than changed silently.",
}

# Every call site that exists today. If the scanner stops seeing one of them
# (a rename, a refactor that hides the call behind a helper), it would pass
# vacuously; this floor makes it fail instead.
KNOWN_CALL_SITES = frozenset({
    ('handlers/tasks.py', 'handle_claude_create_task'),
    ('handlers/triggers.py', '_fire_webhook'),
    ('handlers/triggers.py', 'handle_cron_fire'),
    ('handlers/triggers.py', '_run_page_watch_check'),
    ('handlers/desktop.py', 'handle_desktop_launch'),
    ('server.py', '_start_worker'),
})


def _is_create_task_call(node: ast.Call) -> bool:
    """`ClaudeTaskManager.create_task(...)`, however it is reached
    (`handlers.server.ClaudeTaskManager.create_task`, a bare
    `ClaudeTaskManager.create_task`). Matching the receiver keeps asyncio's
    `loop.create_task` out."""
    fn = node.func
    if not (isinstance(fn, ast.Attribute) and fn.attr == 'create_task'):
        return False
    recv = fn.value
    name = (recv.attr if isinstance(recv, ast.Attribute)
            else recv.id if isinstance(recv, ast.Name) else None)
    return name == 'ClaudeTaskManager'


class _Finder(ast.NodeVisitor):
    def __init__(self):
        self.stack = []
        self.calls = []   # (enclosing function, lineno, passes auto_approve)

    def _visit_def(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_def
    visit_AsyncFunctionDef = _visit_def

    def visit_Call(self, node):
        if _is_create_task_call(node):
            explicit = any(kw.arg == 'auto_approve' for kw in node.keywords)
            self.calls.append((self.stack[-1] if self.stack else '<module>',
                               node.lineno, explicit))
        self.generic_visit(node)


def _scan():
    """{(module, function): [(lineno, passes auto_approve), ...]}"""
    found = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith('.py') or fname.endswith('_test.py'):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=rel)
            finder = _Finder()
            finder.visit(tree)
            for func, lineno, explicit in finder.calls:
                found.setdefault((rel, func), []).append((lineno, explicit))
    return found


class CreateTaskAutoApproveContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.found = _scan()

    def test_every_create_task_call_decides_auto_approve_explicitly(self):
        silent = [f'{mod}:{line} in {func}()'
                  for (mod, func), calls in sorted(self.found.items())
                  if (mod, func) not in ALLOWED_TO_OMIT
                  for line, explicit in calls if not explicit]
        self.assertEqual(
            silent, [],
            'create_task defaults to auto_approve=False. Pass it explicitly: '
            'ClaudeTaskManager.resolve_auto_approve(source) for a launch '
            'nobody watches, or a literal with a comment saying why (#769).')

    def test_scanner_still_sees_every_known_call_site(self):
        missing = sorted(KNOWN_CALL_SITES - set(self.found))
        self.assertEqual(
            missing, [],
            'the scanner no longer finds these create_task call sites; fix '
            'the scanner, or update KNOWN_CALL_SITES if the call really moved')

    def test_allowlist_has_no_stale_entries(self):
        stale = sorted(
            key for key in ALLOWED_TO_OMIT
            if key not in self.found
            or all(explicit for _, explicit in self.found[key]))
        self.assertEqual(
            stale, [],
            'these call sites now pass auto_approve (or are gone); drop them '
            'from ALLOWED_TO_OMIT')


if __name__ == '__main__':
    unittest.main()
