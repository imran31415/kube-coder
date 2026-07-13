"""Antigravity (agy) skill provider — disabled stub.

Antigravity's on-disk skill layout is not yet runtime-verified, so this
provider ships disabled and scan() short-circuits to []. Flip it on
with KC_SKILLS_ANTIGRAVITY=1 once a dev-pod `ls ~/.antigravity`
confirms the layout; scan_roots() below is the speculative guess and
the single place to correct.
"""

from __future__ import annotations

import os
from typing import List, Tuple

from . import SkillProvider


class AntigravityProvider(SkillProvider):
    key = 'antigravity'
    enabled = os.environ.get('KC_SKILLS_ANTIGRAVITY', '') == '1'

    def scan_roots(self) -> List[Tuple[str, str]]:
        return [
            ('user', os.path.join(home, '.antigravity', 'skills'))
            for home in ('/home/dev', '/home/ubuntu', os.path.expanduser('~'))
        ]
