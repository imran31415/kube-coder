#!/usr/bin/env python3
"""Run the workspace Python suite with isolated live stores (Linux required)."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

if sys.platform != 'linux':
    raise SystemExit('Run this gate on Linux/WSL; real flock is required.')
workspace = Path(__file__).resolve().parents[1] / 'charts' / 'workspace'
os.chdir(workspace)
sys.path.insert(0, str(workspace))
with tempfile.TemporaryDirectory(prefix='kc-publish-suite-') as scratch:
    for key, name in [('KC_FEED_DIR', 'feed'), ('KC_PUSH_DIR', 'push'),
                      ('KC_PROVIDER_KEYS_FILE', 'provider-keys.json'),
                      ('KC_TRIGGER_RUNS_DIR', 'runs'), ('KC_PUBLISH_DIR', 'publish')]:
        os.environ[key] = str(Path(scratch) / name)
    suite = unittest.defaultTestLoader.discover('tests', pattern='*_test.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0:
        raise SystemExit('No tests discovered')
    raise SystemExit(0 if result.wasSuccessful() else 1)
