"""The board credential store (#588 Phase 4).

Two things are being proven here, and the second matters more than the first.

1. The store works: names are validated, `basic` composes the header the way
   Jira Cloud needs it, a blank secret on update preserves the stored one, and
   nothing but `get_raw` ever returns a value.

2. **A board credential does not reach an agent process.** That is the whole
   reason this store exists rather than five more entries in
   `ProviderKeysManager.ALLOWED` — provider keys are injected into every CLI
   subprocess's env at spawn, so storing a Jira token there would hand it to
   every agent on the workspace. `test_a_board_credential_is_in_no_env_overlay`
   is the regression guard on that decision.

Run:  python3 -m unittest tests.boards_credentials_test   (from charts/workspace/)
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# server.py imports fcntl (Unix-only) at module load — shim for non-Unix dev.
try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.lockf = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import server  # noqa: E402
from boards import schema  # noqa: E402

BCM = server.BoardCredentialsManager
BM = server.BoardsManager


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix='kc-boardcred-'))
        self._saved = BCM.HOME_ROOT
        BCM.HOME_ROOT = self.tmpdir
        self.addCleanup(setattr, BCM, 'HOME_ROOT', self._saved)
        self.addCleanup(shutil.rmtree, self.tmpdir, True)


class StoreTests(_Base):
    def test_token_round_trips(self):
        ok, err = BCM.set('JIRA_API_TOKEN', 'tok-123')
        self.assertTrue(ok, err)
        value, err = BCM.get_raw('JIRA_API_TOKEN')
        self.assertIsNone(err)
        self.assertEqual(value, 'tok-123')

    def test_basic_composes_the_header_value_from_email_and_raw_token(self):
        """Jira Cloud is Basic auth over `email:api_token`. The user pastes the
        RAW token; asking for a pre-encoded blob is a footgun — it is
        unverifiable by eye and a stray newline from a shell `base64` produces
        a credential that fails only at request time."""
        ok, err = BCM.set('JIRA', 'tok-123', fmt='basic', username='me@x.com')
        self.assertTrue(ok, err)
        value, err = BCM.get_raw('JIRA')
        self.assertIsNone(err)
        self.assertEqual(base64.b64decode(value).decode(), 'me@x.com:tok-123')

    def test_basic_without_a_username_is_refused_at_save_time(self):
        # base64(":token") authenticates as nobody and 401s in a way that reads
        # like a bad token. Refuse where the user can still fix it.
        ok, err = BCM.set('JIRA', 'tok', fmt='basic')
        self.assertFalse(ok)
        self.assertIn('username is required', err)

    def test_blank_secret_on_update_keeps_the_stored_one(self):
        BCM.set('JIRA', 'tok-123', fmt='basic', username='old@x.com')
        ok, err = BCM.set('JIRA', '', fmt='basic', username='new@x.com')
        self.assertTrue(ok, err)
        value, _ = BCM.get_raw('JIRA')
        self.assertEqual(base64.b64decode(value).decode(), 'new@x.com:tok-123')

    def test_blank_secret_on_a_NEW_name_is_refused(self):
        ok, err = BCM.set('BRAND_NEW', '')
        self.assertFalse(ok)
        self.assertIn('secret is required', err)

    def test_bad_names_are_refused(self):
        for name in ('', 'jira', 'AB', '1JIRA', 'JIRA-TOKEN', 'A' * 65,
                     'JIRA TOKEN', '../etc/passwd'):
            with self.subTest(name=name):
                ok, _err = BCM.set(name, 'x')
                self.assertFalse(ok)

    def test_unknown_format_is_refused(self):
        ok, err = BCM.set('JIRA', 'x', fmt='oauth2')
        self.assertFalse(ok)
        self.assertIn('format must be', err)

    def test_missing_credential_reports_where_to_add_it(self):
        value, err = BCM.get_raw('NOPE')
        self.assertEqual(value, '')
        self.assertIn('NOPE', err)
        self.assertIn('Boards', err)

    def test_delete_is_reported_honestly(self):
        BCM.set('JIRA', 'x')
        self.assertTrue(BCM.delete('JIRA'))
        self.assertFalse(BCM.delete('JIRA'))
        self.assertEqual(BCM.public_view(), [])

    def test_created_at_survives_an_update(self):
        BCM.set('JIRA', 'a')
        first = BCM.public_view()[0]['created_at']
        BCM.set('JIRA', 'b')
        self.assertEqual(BCM.public_view()[0]['created_at'], first)

    def test_a_corrupt_store_reads_as_empty_rather_than_raising(self):
        os.makedirs(os.path.dirname(BCM.creds_file()), exist_ok=True)
        with open(BCM.creds_file(), 'w') as f:
            f.write('{not json')
        self.assertEqual(BCM.public_view(), [])
        self.assertEqual(BCM.get_raw('JIRA')[0], '')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX file modes')
    def test_the_file_is_0600(self):
        BCM.set('JIRA', 'x')
        self.assertEqual(os.stat(BCM.creds_file()).st_mode & 0o777, 0o600)


class DisclosureTests(_Base):
    def test_public_view_shows_a_last4_hint_and_never_the_value(self):
        BCM.set('JIRA', 'super-secret-abcd', fmt='basic', username='me@x.com')
        view = BCM.public_view()
        self.assertEqual(len(view), 1)
        entry = view[0]
        self.assertEqual(entry['name'], 'JIRA')
        self.assertEqual(entry['format'], 'basic')
        self.assertEqual(entry['username'], 'me@x.com')
        self.assertEqual(entry['hint'], '…abcd')
        self.assertNotIn('super-secret-abcd', json.dumps(view))
        self.assertNotIn('secret', entry)

    def test_a_short_secret_gets_no_hint_rather_than_leaking_most_of_itself(self):
        BCM.set('JIRA', 'abc')
        self.assertEqual(BCM.public_view()[0]['hint'], '')

    def test_a_board_credential_is_in_no_env_overlay(self):
        """The reason this store is separate from ProviderKeysManager.

        Provider keys are applied to every CLI subprocess's env at spawn. A
        board credential must never travel that path — the Board Processor's
        discipline is that an agent NAMES a credential and never sees it.
        """
        BCM.set('JIRA_API_TOKEN', 'board-only-secret')
        overlay = server.ProviderKeysManager.env_overlay()
        self.assertNotIn('JIRA_API_TOKEN', overlay)
        self.assertNotIn('board-only-secret', json.dumps(overlay))
        self.assertNotIn('JIRA_API_TOKEN', server.ProviderKeysManager.ALLOWED)

    def test_the_store_does_not_live_where_boards_are_listed(self):
        """.claude-boards/*.json is enumerated as connectors, so a credentials
        file in there would render in the UI as a board named 'credentials'."""
        self.assertNotIn(os.path.normpath(BM.boards_dir()),
                         os.path.normpath(BCM.creds_file()))


class ResolutionTests(_Base):
    """`BoardsManager._credential_for` is the ONLY caller of get_raw."""

    def test_board_creds_reference_resolves(self):
        BCM.set('JIRA_API_TOKEN', 'tok-123')
        value, err = BM._credential_for('@board-creds/JIRA_API_TOKEN')
        self.assertIsNone(err)
        self.assertEqual(value, 'tok-123')

    def test_an_empty_reference_means_a_public_board(self):
        self.assertEqual(BM._credential_for(''), ('', None))

    def test_an_unknown_reference_scheme_is_refused(self):
        _v, err = BM._credential_for('@aws-secrets/JIRA')
        self.assertIn('unknown credential reference', err)

    def test_an_inline_secret_is_never_accepted_as_a_reference(self):
        _v, err = BM._credential_for('ghp_realtokenvalue')
        self.assertTrue(err)

    def test_schema_accepts_board_creds_and_still_accepts_the_legacy_form(self):
        for ref in ('@board-creds/JIRA_API_TOKEN',
                    '@provider-keys/ANTHROPIC_API_KEY',
                    '@workspace-github', '', None):
            with self.subTest(ref=ref):
                ok, err = schema.validate_credential_ref(ref)
                self.assertTrue(ok, err)

    def test_schema_rejects_a_lowercase_name(self):
        ok, _err = schema.validate_credential_ref('@board-creds/jira_token')
        self.assertFalse(ok)

    def test_reserved_ids_cannot_shadow_a_route(self):
        for bad in schema.RESERVED_BOARD_IDS:
            with self.subTest(board_id=bad):
                _cfg, err = BM.create_or_update({'id': bad})
                self.assertIn('reserved', err)


if __name__ == '__main__':
    unittest.main()
