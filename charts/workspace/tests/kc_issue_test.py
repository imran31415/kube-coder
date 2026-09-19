"""`kc-issue` works on any repository and hands worktree creation to the
server when the pod can do it (#701).

Driven end to end through the ad-hoc (`<slug> "desc"`) path, which needs no
GitHub: a real repository with a real `origin`, the real script, the real
worktree helper. Needs bash, git and jq — the pod has all three, and so do CI's
runners; a machine without jq skips.

Run:  python3 -m unittest tests.kc_issue_test   (from charts/workspace/)
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tests import git_fixtures as gf  # noqa: E402
from tests.git_fixtures import git, make_remote, make_repo  # noqa: E402

SKILLS = os.path.realpath(os.path.join(HERE, '..', '..', '..', '.claude', 'skills'))
SCRIPT = os.path.join(SKILLS, 'kc-issue', 'kc-issue.sh')
WORKTREES_PY = os.path.realpath(os.path.join(HERE, '..', 'worktrees.py'))
BASH = shutil.which('bash')
DESC = ('Fix the widget in src/app.py so the health endpoint returns 200.\n\n'
        '## Acceptance criteria\n- [ ] GET /health returns 200\n\n'
        '## Verification\nRun the unit tests.')


@gf.requires_git
@gf.posix_only
@unittest.skipUnless(BASH and shutil.which('jq'), 'kc-issue needs bash and jq')
class KcIssueTests(gf.GitTestCase):

    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.home, 'widget')
        make_remote(self.tmp, self.repo)
        git(self.repo, 'push', '-q', 'origin', 'main')
        git(self.repo, 'remote', 'set-head', 'origin', 'main')
        self.scratch = os.path.join(self.tmp, 'scratch')
        os.makedirs(self.scratch)

    def run_kc(self, *args, server=True, cwd=None, extra=None):
        env = dict(os.environ, TMPDIR=self.scratch, KC_REPO_SLUG='acme/widget',
                   KC_WORKTREES_PY=WORKTREES_PY if server else '/nonexistent.py')
        env.pop('KC_REPO_ROOT', None)
        env.update(extra or {})
        proc = subprocess.run([BASH, SCRIPT, *args], capture_output=True,
                              text=True, cwd=cwd or self.repo, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_server_mode_leaves_the_worktree_to_the_dashboard(self):
        out = self.run_kc('widget-fix', DESC)
        self.assertEqual((out['mode'], out['slug'], out['branch'], out['base_ref'],
                          out['repo'], out['repo_root']),
                         ('server', 'widget-fix', 'kc/widget-fix', 'origin/main',
                          'acme/widget', self.repo))
        # Nothing was created; the server makes it at launch.
        self.assertFalse(os.path.exists(self.wt_root))
        self.assertTrue(out['prompt_file'].startswith(self.scratch))
        with open(out['prompt_file'], encoding='utf-8') as f:
            prompt = f.read()
        self.assertIn('issue #widget-fix of acme/widget', prompt)
        self.assertIn('$KC_WT', prompt)
        self.assertIn('$PORT', prompt)
        # Not the kube-coder repo, so its skills are not named.
        self.assertNotIn('kc-preflight', prompt)
        self.assertIn("this repository's tests", prompt)

    def test_script_mode_still_makes_the_worktree_itself(self):
        out = self.run_kc('widget-fix', DESC, server=False)
        self.assertEqual(out['mode'], 'script')
        self.assertTrue(os.path.isdir(out['worktree']))
        self.assertEqual(git(out['worktree'], 'rev-parse', '--abbrev-ref', 'HEAD'),
                         'kc/widget-fix')
        self.assertEqual(out['prompt_file'],
                         os.path.join(out['worktree'], '.kc-issue-prompt.md'))
        with open(out['prompt_file'], encoding='utf-8') as f:
            self.assertIn(f"path:   {out['worktree']}", f.read())
        # Re-running reuses it.
        again = self.run_kc('widget-fix', DESC, server=False)
        self.assertEqual(again['worktree'], out['worktree'])

    def test_the_repository_is_found_from_a_subfolder_and_a_linked_worktree(self):
        os.makedirs(os.path.join(self.repo, 'src'))
        self.assertEqual(self.run_kc('a-fix', DESC, cwd=os.path.join(self.repo, 'src'))
                         ['repo_root'], self.repo)
        linked = os.path.join(self.tmp, 'linked')
        git(self.repo, 'worktree', 'add', '-q', '-b', 'side', linked)
        self.assertEqual(self.run_kc('b-fix', DESC, cwd=linked)['repo_root'], self.repo)

    def test_the_github_slug_comes_from_origin(self):
        fn = ('eval "$(sed -n \'/^detect_repo_slug()/,/^}/p\' "$0")"; '
              'REPO_ROOT="$1"; unset KC_REPO_SLUG; detect_repo_slug')
        for url, want in (('git@github.com:acme/widget.git', 'acme/widget'),
                          ('https://github.com/acme/widget.git', 'acme/widget'),
                          ('https://github.com/acme/widget', 'acme/widget')):
            git(self.repo, 'remote', 'set-url', 'origin', url)
            proc = subprocess.run([BASH, '-c', fn, SCRIPT, self.repo],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.stdout, want, url)

    def test_the_kube_coder_skills_are_named_in_a_repo_that_has_them(self):
        for skill in ('kc-preflight', 'kc-ship-pr'):
            os.makedirs(os.path.join(self.repo, '.claude', 'skills', skill))
        out = self.run_kc('k-fix', DESC, extra={'KC_AUTO_PR': '1'})
        with open(out['prompt_file'], encoding='utf-8') as f:
            prompt = f.read()
        self.assertIn('kc-preflight', prompt)
        self.assertIn('kc-ship-pr', prompt)
        self.assertIn('Fixes #k-fix', prompt)

    def test_the_lint_suite_still_passes(self):
        proc = subprocess.run([BASH, os.path.join(SKILLS, 'kc-issue', 'lint_test.sh')],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == '__main__':
    unittest.main()
