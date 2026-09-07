"""The repo's OWN .claude/skills/ files parse and carry what a harness needs.

Distinct from skills_test.py, which covers the runtime discovery package. This
covers the skills this repository ships to describe itself — kc-preflight,
kc-ship-pr, kc-scope-pr and friends.

A malformed SKILL.md does not raise anywhere: the harness simply does not offer
the skill, and the first sign is a person wondering why `/kc-scope-pr` is not
in the list. That silence is what this pins. It reuses the shipped parser
rather than a second one, so a skill that passes here is one the runtime can
actually read.

Run with:  python3 -m unittest tests.repo_skills_test  (from charts/workspace/)
"""

import os
import sys
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE.parent))
from skills.parser import parse_frontmatter  # noqa: E402

SKILLS_DIR = HERE.parent.parent.parent / '.claude' / 'skills'


def skill_files():
    return sorted(SKILLS_DIR.glob('*/SKILL.md'))


class RepoSkillsTests(unittest.TestCase):
    def test_there_are_skills_to_check(self):
        # Guards the guard: a wrong SKILLS_DIR would make every test below
        # pass over an empty list and prove nothing.
        self.assertTrue(skill_files(), f'no SKILL.md under {SKILLS_DIR}')

    def test_every_skill_parses_with_the_shipped_parser(self):
        for f in skill_files():
            with self.subTest(skill=f.parent.name):
                meta, body = parse_frontmatter(f.read_text())
                self.assertTrue(meta, 'no frontmatter parsed')
                self.assertTrue(body.strip(), 'no body after the frontmatter')

    def test_name_and_description_are_present(self):
        # `description` is the whole routing signal — a skill without one is
        # invisible to the model that has to decide whether to use it.
        for f in skill_files():
            with self.subTest(skill=f.parent.name):
                meta, _ = parse_frontmatter(f.read_text())
                self.assertTrue(meta.get('name'), 'missing name')
                self.assertTrue(meta.get('description'), 'missing description')

    def test_the_name_matches_its_directory(self):
        # They are addressed by directory; a mismatch means /name does not
        # invoke what the file claims to be.
        for f in skill_files():
            with self.subTest(skill=f.parent.name):
                meta, _ = parse_frontmatter(f.read_text())
                self.assertEqual(meta['name'], f.parent.name)

    def test_pr_skills_route_to_each_other(self):
        """Scope -> preflight -> ship is the intended order, and each step is
        only findable from the one before it."""
        text = {f.parent.name: f.read_text() for f in skill_files()}
        self.assertIn('kc-scope-pr', text, 'the scoping skill is missing')
        self.assertIn('kc-scope-pr', text['kc-preflight'],
                      'kc-preflight should point back at scoping')
        self.assertIn('kc-scope-pr', text['kc-ship-pr'],
                      'kc-ship-pr should point back at scoping')
        self.assertIn('kc-preflight', text['kc-scope-pr'],
                      'kc-scope-pr should hand off to preflight')
