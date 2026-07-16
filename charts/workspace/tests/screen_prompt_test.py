"""Unit tests for the interactive-prompt screen parser (issue #204).

Covers parse_screen_prompt over representative captured Claude Code TUI screens:
the numbered permission menu (bordered and plain), free-form yes/no prompts, and
the negative cases that MUST return None (ordinary output, prose numbered lists
with no selection caret, blank screens) so the plain composer stays in control.

Run with:    python3 -m unittest tests.screen_prompt_test
(from charts/workspace/)
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

parse = server.parse_screen_prompt


# A bordered 3-option permission prompt as tmux capture-pane -J renders it
# (box drawing borders, the ❯ selection caret on option 1). This is the exact
# shape Claude Code shows before running a tool.
PERMISSION_BORDERED = """\
● I'll run the build for you.

╭──────────────────────────────────────────────────────────────╮
│ Bash command                                                   │
│                                                                │
│   npm run build                                                │
│                                                                │
│ Do you want to proceed?                                        │
│ ❯ 1. Yes                                                       │
│   2. Yes, and don't ask again for npm commands in this project │
│   3. No, and tell Claude what to do differently (esc)          │
╰──────────────────────────────────────────────────────────────╯
"""

# The same menu without borders (some widths / terminals render it flat).
PERMISSION_PLAIN = """\
Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again
  3. No, and tell Claude what to do differently
"""

# Caret sitting on option 2 (user arrowed down before we captured).
PERMISSION_CARET_ON_TWO = """\
Do you want to proceed?
  1. Yes
❯ 2. Yes, and don't ask again
  3. No
"""

# The trust-folder dialog — a 2-option numbered prompt.
TRUST_FOLDER = """\
╭──────────────────────────────────────────╮
│ Do you trust the files in this folder?     │
│                                            │
│ ❯ 1. Yes, proceed                          │
│   2. No, exit                              │
╰──────────────────────────────────────────╯
"""

# A free-form yes/no question with the (y/n) marker.
YESNO_INLINE = """\
Overwrite existing file config.yaml? (y/n)
"""

# yes/no where the marker sits on its own line under the question.
YESNO_SPLIT = """\
This will delete 12 files.
Continue?
[Y/n]
"""

# Ordinary running output — no prompt at all.
RUNNING_OUTPUT = """\
● Reading src/server.py
● Editing src/server.py
  Updated 3 functions

Running tests...
  ok 12 passed
"""

# Prose numbered list Claude printed as CONTENT (no selection caret) — must NOT
# be mistaken for a permission menu.
PROSE_LIST = """\
Here's my plan:
1. Add the parser to server.py
2. Wire it into get_task
3. Add unit tests

Let me start.
"""


class NumberedChoiceTests(unittest.TestCase):
    def test_bordered_permission_prompt(self):
        r = parse(PERMISSION_BORDERED)
        self.assertIsNotNone(r)
        self.assertEqual(r['kind'], 'choice')
        self.assertEqual(r['question'], 'Do you want to proceed?')
        self.assertEqual([o['index'] for o in r['options']], [1, 2, 3])
        self.assertEqual(r['options'][0]['label'], 'Yes')
        self.assertEqual(
            r['options'][1]['label'],
            "Yes, and don't ask again for npm commands in this project")
        # Box borders must be stripped from labels.
        self.assertNotIn('│', r['options'][2]['label'])

    def test_plain_permission_prompt(self):
        r = parse(PERMISSION_PLAIN)
        self.assertEqual(r['kind'], 'choice')
        self.assertEqual(r['question'], 'Do you want to proceed?')
        self.assertEqual(len(r['options']), 3)
        self.assertEqual(r['options'][0]['label'], 'Yes')

    def test_caret_on_non_first_option(self):
        r = parse(PERMISSION_CARET_ON_TWO)
        self.assertEqual(r['kind'], 'choice')
        self.assertEqual([o['index'] for o in r['options']], [1, 2, 3])
        self.assertEqual(r['options'][1]['label'], "Yes, and don't ask again")

    def test_two_option_trust_dialog(self):
        r = parse(TRUST_FOLDER)
        self.assertEqual(r['kind'], 'choice')
        self.assertEqual(r['question'], 'Do you trust the files in this folder?')
        self.assertEqual([o['label'] for o in r['options']],
                         ['Yes, proceed', 'No, exit'])


class YesNoTests(unittest.TestCase):
    def test_inline_marker(self):
        r = parse(YESNO_INLINE)
        self.assertEqual(r['kind'], 'yesno')
        self.assertEqual(r['question'], 'Overwrite existing file config.yaml?')
        self.assertEqual([o['index'] for o in r['options']], ['y', 'n'])
        self.assertEqual([o['label'] for o in r['options']], ['Yes', 'No'])

    def test_marker_on_own_line_uses_line_above(self):
        r = parse(YESNO_SPLIT)
        self.assertEqual(r['kind'], 'yesno')
        self.assertEqual(r['question'], 'Continue?')


class NegativeTests(unittest.TestCase):
    def test_ordinary_output_returns_none(self):
        self.assertIsNone(parse(RUNNING_OUTPUT))

    def test_prose_numbered_list_returns_none(self):
        # No selection caret ⇒ not a live menu ⇒ no buttons.
        self.assertIsNone(parse(PROSE_LIST))

    def test_empty_and_blank(self):
        self.assertIsNone(parse(''))
        self.assertIsNone(parse(None))
        self.assertIsNone(parse('   \n\n   '))

    def test_single_numbered_option_is_not_a_menu(self):
        # A lone "❯ 1. Yes" (needs >= 2 options) should not render buttons.
        self.assertIsNone(parse('Proceed?\n❯ 1. Yes\n'))

    def test_non_sequential_numbers_rejected(self):
        # Caret present but indices don't start at 1 / aren't sequential.
        self.assertIsNone(parse('Pick:\n❯ 2. A\n  4. B\n'))


if __name__ == '__main__':
    unittest.main()
