"""Runtime signup allowlist: state, console use cases and eligibility.

No cluster, GitHub or Dex: the store is signup_test.MemoryStore and GitHub is a
small fake directory behind the backend's lookup.
"""
import os
import re
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signup_allowlist
from observability_metrics import Registry
from provision_store import Conflict
from signup_allowlist import RECORD, Allowlist, AllowlistConsole
from signup_errors import SignupError
from signup_test import MemoryStore, fixture

GITHUB = {
    'mona': {'id': 456, 'login': 'Mona', 'type': 'User', 'name': 'Mona Lisa', 'avatar_url': 'https://a.test/456'},
    'octocat': {'id': 123, 'login': 'octocat', 'type': 'User', 'name': '', 'avatar_url': ''},
    'acme': {'id': 789, 'login': 'acme', 'type': 'Organization'},
}


def github_lookup(login):
    """Same contract as provision_backend.Backend.lookup."""
    if not re.fullmatch(r'[a-zA-Z0-9](?:-?[a-zA-Z0-9]){0,38}', login):
        raise SignupError('invalid_request')
    if login.lower() not in GITHUB:
        raise SignupError('github_user_not_found')
    return dict(GITHUB[login.lower()])


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RacingStore(MemoryStore):
    """Another replica writes between our read and our save, `races` times."""

    def __init__(self, races):
        super().__init__()
        self.races, self.saves = races, 0

    def save(self, name, value):
        self.saves += 1
        if name == RECORD and self.races:
            self.races -= 1
            rival = self.get(RECORD) or {'entries': {}}
            rival['entries'][str(900 + self.races)] = {'login': 'rival', 'addedBy': 'bob', 'addedAt': 1, 'note': ''}
            super().save(name, rival)
        return super().save(name, value)


class FailingStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.down = False

    def get(self, name):
        if self.down:
            raise SignupError('state_unavailable')
        return super().get(name)


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        self.store, self.clock = MemoryStore(), Clock()
        self.allowlist = Allowlist(self.store, {'123'}, clock=self.clock)

    def test_seed_and_console_entries_are_both_allowed(self):
        self.assertTrue(self.allowlist.allows('123'))
        self.assertFalse(self.allowlist.allows('456'))
        self.allowlist.add('456', 'Mona', 'alice')
        self.assertTrue(self.allowlist.allows('456'))

    def test_entries_list_console_newest_first_then_readonly_helm(self):
        self.allowlist.add('456', 'Mona', 'alice')
        with patch.object(signup_allowlist.time, 'time', return_value=2e9):
            self.allowlist.add('457', 'Hubot', 'alice')
        self.assertEqual([(e['githubId'], e['source']) for e in self.allowlist.entries()],
                         [('457', 'console'), ('456', 'console'), ('123', 'helm')])

    def test_add_is_idempotent_and_records_who_added(self):
        entry, created = self.allowlist.add('456', 'Mona', 'alice', 'Spring intern')
        self.assertTrue(created)
        self.assertEqual((entry['login'], entry['addedBy'], entry['note'], entry['source']),
                         ('Mona', 'alice', 'Spring intern', 'console'))
        again, created = self.allowlist.add('456', 'Mona', 'bob')
        self.assertFalse(created)
        self.assertEqual(again['addedBy'], 'alice')

    def test_adding_a_helm_id_writes_nothing(self):
        entry, created = self.allowlist.add('123', 'octocat', 'alice')
        self.assertFalse(created)
        self.assertEqual(entry['source'], 'helm')
        self.assertNotIn(RECORD, self.store.records)

    def test_invalid_input_is_rejected_without_a_write(self):
        for github_id, note in (('abc', ''), ('0123', ''), ('', ''), (456, ''), ('456', 'x' * 201), ('456', None)):
            with self.subTest(github_id=github_id), self.assertRaises(SignupError) as raised:
                self.allowlist.add(github_id, 'Mona', 'alice', note)
            self.assertEqual(raised.exception.code, 'invalid_request')
        with self.assertRaises(SignupError):
            self.allowlist.remove('../x', 'alice')
        self.assertNotIn(RECORD, self.store.records)

    def test_full_list_refuses_new_entries_but_not_existing_ones(self):
        allowlist = Allowlist(self.store, {'123'}, max_entries=1, clock=self.clock)
        allowlist.add('456', 'Mona', 'alice')
        self.assertFalse(allowlist.add('456', 'Mona', 'alice')[1])
        with self.assertRaises(SignupError) as raised:
            allowlist.add('457', 'Hubot', 'alice')
        self.assertEqual(raised.exception.code, 'allowlist_full')
        self.assertEqual(set(self.store.get(RECORD)['entries']), {'456'})

    def test_helm_ids_do_not_count_against_the_console_cap(self):
        # Stored before it was also put in the Helm seed: it no longer counts.
        Allowlist(self.store, ()).add('123', 'octocat', 'alice')
        allowlist = Allowlist(self.store, {'123'}, max_entries=1, clock=self.clock)
        self.assertTrue(allowlist.add('456', 'Mona', 'alice')[1])

    def test_list_stops_short_of_the_configmap_size_limit_whatever_the_cap(self):
        allowlist = Allowlist(self.store, (), max_entries=10, clock=self.clock)
        with patch.object(signup_allowlist, 'MAX_RECORD_BYTES', 300),                 self.assertLogs('controller', 'WARNING') as logs:
            allowlist.add('456', 'Mona', 'alice', 'a' * 100)   # about 200 bytes: fits
            with self.assertRaises(SignupError) as raised:
                allowlist.add('457', 'Hubot', 'alice', 'b' * 100)
        self.assertEqual(raised.exception.code, 'allowlist_full')
        self.assertEqual(set(self.store.get(RECORD)['entries']), {'456'})
        self.assertIn('signup_allowlist_too_large', ''.join(logs.output))

    def test_remove_and_its_noop_and_readonly_cases(self):
        self.allowlist.add('456', 'Mona', 'alice')
        self.assertEqual(self.allowlist.remove('456', 'alice')['login'], 'Mona')
        self.assertFalse(self.allowlist.allows('456'))
        self.assertIsNone(self.allowlist.remove('456', 'alice'))
        with self.assertRaises(SignupError) as raised:
            self.allowlist.remove('123', 'alice')
        self.assertEqual(raised.exception.code, 'allowlist_readonly')
        self.assertTrue(self.allowlist.allows('123'))

    def test_concurrent_write_is_retried_and_both_survive(self):
        store = RacingStore(races=1)
        allowlist = Allowlist(store, ())
        _, created = allowlist.add('456', 'Mona', 'alice')
        self.assertTrue(created)
        self.assertEqual(set(store.get(RECORD)['entries']), {'456', '900'})

    def test_remove_retry_sees_the_rivals_write(self):
        store = RacingStore(races=0)
        allowlist = Allowlist(store, ())
        allowlist.add('456', 'Mona', 'alice')
        store.races = 1
        self.assertIsNotNone(allowlist.remove('456', 'alice'))
        self.assertEqual(set(store.get(RECORD)['entries']), {'900'})

    def test_conflicts_pause_briefly_and_randomly_before_retrying(self):
        store = RacingStore(races=2)
        with patch.object(signup_allowlist.time, 'sleep') as sleep:
            Allowlist(store, ()).add('456', 'Mona', 'alice')
        pauses = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(pauses), 2)
        for attempt, pause in enumerate(pauses, 1):
            self.assertTrue(0 <= pause <= signup_allowlist.RETRY_PAUSE * attempt)

    def test_a_burst_of_adds_across_two_replicas_all_land(self):
        class SlowStore(MemoryStore):
            def save(self, name, value):
                time.sleep(0.002)   # a real API round trip leaves room to interleave
                return super().save(name, value)
        store = SlowStore()
        replicas = (Allowlist(store, ()), Allowlist(store, ()))
        created = []
        threads = [threading.Thread(target=lambda i=i: created.append(
            replicas[i % 2].add(str(1000 + i), f'user{i}', 'alice')[1])) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(created, [True] * 20)
        self.assertEqual(len(store.get(RECORD)['entries']), 20)

    def test_persistent_conflict_gives_busy_without_a_partial_write(self):
        store = RacingStore(races=signup_allowlist.WRITE_ATTEMPTS)
        allowlist = Allowlist(store, ())
        with patch.object(signup_allowlist.time, 'sleep') as sleep, self.assertRaises(SignupError) as raised:
            allowlist.add('456', 'Mona', 'alice')
        self.assertEqual(sleep.call_count, signup_allowlist.WRITE_ATTEMPTS - 1)   # no pause after the last try
        self.assertEqual(raised.exception.code, 'busy')
        self.assertNotIn('456', store.get(RECORD)['entries'])

    def test_other_replica_sees_a_change_after_the_cache_expires(self):
        other = Allowlist(self.store, {'123'}, clock=self.clock)
        self.assertFalse(other.allows('456'))
        self.allowlist.add('456', 'Mona', 'alice')
        self.assertTrue(self.allowlist.allows('456'))   # own write: at once
        self.assertFalse(other.allows('456'))           # cached up to 5 s
        self.assertTrue(other.allows('456', fresh=True))
        other = Allowlist(self.store, {'123'}, clock=self.clock)
        other.allows('456')
        self.allowlist.remove('456', 'alice')
        self.clock.now += signup_allowlist.CACHE_SECONDS
        self.assertFalse(other.allows('456'))

    def test_store_failure_fails_closed_even_with_a_warm_cache(self):
        store = FailingStore()
        allowlist = Allowlist(store, {'123'}, clock=self.clock)
        allowlist.add('456', 'Mona', 'alice')
        store.down = True
        self.assertTrue(allowlist.allows('456'))   # within the cache window
        self.assertTrue(allowlist.allows('123'))   # the seed never needs the store
        for call in (lambda: allowlist.allows('456', fresh=True), allowlist.entries,
                     lambda: allowlist.add('457', 'Hubot', 'alice')):
            with self.assertRaises(SignupError) as raised:
                call()
            self.assertEqual(raised.exception.code, 'state_unavailable')
        self.clock.now += signup_allowlist.CACHE_SECONDS
        with self.assertRaises(SignupError):
            allowlist.allows('456')

    def test_hand_edited_record_is_refused_not_overwritten(self):
        for broken in ({'entries': []}, {'entries': {'octocat': {}}}, {'entries': {'456': 'x'}}, {}):
            with self.subTest(broken=broken):
                store = MemoryStore()
                store.save(RECORD, broken)
                allowlist = Allowlist(store, ())
                with self.assertLogs('controller', 'INFO') as logs, self.assertRaises(SignupError):
                    allowlist.add('456', 'Mona', 'alice')
                self.assertEqual(logs.records[-1].event, 'signup_allowlist_invalid')
                self.assertEqual(store.get(RECORD)['_version'], '1')

    def test_audit_events_name_the_actor(self):
        with self.assertLogs('controller', 'INFO') as logs:
            self.allowlist.add('456', 'Mona', 'alice')
            self.allowlist.add('456', 'Mona', 'alice')
            self.allowlist.remove('456', 'bob')
            self.allowlist.remove('456', 'bob')
        audit = [(r.event, r.fields) for r in logs.records]
        self.assertEqual(audit, [
            ('signup_allowlist_added', {'github_id': '456', 'login': 'Mona', 'actor': 'alice'}),
            ('signup_allowlist_removed', {'github_id': '456', 'login': 'Mona', 'actor': 'bob'})])

    def test_gauge_reports_both_sources(self):
        registry = Registry()
        with patch.object(signup_allowlist, 'REGISTRY', registry):
            self.allowlist.add('456', 'Mona', 'alice')
        output = registry.exposition()
        self.assertIn('kubecoder_signup_allowlist_entries{source="helm"} 1', output)
        self.assertIn('kubecoder_signup_allowlist_entries{source="console"} 1', output)


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        self.service = fixture()
        self.service.backend.lookup.side_effect = github_lookup
        self.console = AllowlistConsole(self.service, 'https://signup.example.test')

    def add(self, login='mona', github_id='456', **extra):
        return self.console.add({'login': login, 'githubId': github_id, **extra}, 'alice')

    def test_lookup_shows_the_account_and_whether_it_is_allowed(self):
        account = self.console.lookup('mona')
        self.assertEqual(account, {'githubId': '456', 'login': 'Mona', 'name': 'Mona Lisa',
                                   'avatarUrl': 'https://a.test/456', 'allowed': False,
                                   'source': None, 'workspace': None})
        self.add()
        self.assertEqual(self.console.lookup('mona')['source'], 'console')
        self.assertEqual(self.console.lookup('octocat')['source'], 'helm')

    def test_lookup_rejects_organizations_and_passes_github_errors_through(self):
        for login, code in (('acme', 'not_a_person'), ('nobody', 'github_user_not_found')):
            with self.subTest(login=login), self.assertRaises(SignupError) as raised:
                self.console.lookup(login)
            self.assertEqual(raised.exception.code, code)

    def test_add_stores_the_id_and_rechecks_the_username(self):
        result = self.add(note='  Spring intern  ')
        self.assertEqual((result['created'], result['entry']['githubId'], result['entry']['note']),
                         (True, '456', 'Spring intern'))
        with self.assertRaises(SignupError) as raised:
            self.add(github_id='999')
        self.assertEqual(raised.exception.code, 'github_user_changed')
        self.assertEqual(set(self.service.store.get(RECORD)['entries']), {'456'})

    def test_add_rejects_malformed_bodies_without_calling_github(self):
        for body in ([], {'login': 'mona'}, {'login': 'mona', 'githubId': 456},
                     {'login': 'mona', 'githubId': '456', 'admin': True},
                     {'login': None, 'githubId': '456'}, {'login': 'mona', 'githubId': '456', 'note': 5}):
            with self.subTest(body=body), self.assertRaises(SignupError) as raised:
                self.console.add(body, 'alice')
            self.assertEqual(raised.exception.code, 'invalid_request')
        self.service.backend.lookup.assert_not_called()

    def test_overview_reports_the_configured_cap(self):
        self.service.allowlist = Allowlist(self.service.store, {'123'}, max_entries=25)
        self.assertEqual(AllowlistConsole(self.service).overview()['limit'], 25)

    def test_add_refuses_organizations(self):
        with self.assertRaises(SignupError) as raised:
            self.add(login='acme', github_id='789')
        self.assertEqual(raised.exception.code, 'not_a_person')

    def test_overview_attaches_workspaces_and_hides_deleted_ones(self):
        self.add()
        self.service.create('456', 'Mona')
        overview = self.console.overview()
        self.assertEqual((overview['limit'], overview['noteMax'], overview['signupUrl']),
                         (1000, 200, 'https://signup.example.test'))
        mona, octocat = overview['entries']
        self.assertEqual(mona['workspace'], {'slug': 'mona', 'stage': 'identity'})
        self.assertEqual((octocat['source'], octocat['login'], octocat['workspace']), ('helm', None, None))
        self.service.create('123', 'octocat')
        record = self.service.get('456')
        record['stage'] = 'deleted'
        self.service.store.save('owner-456', record)
        mona, octocat = self.console.overview()['entries']
        self.assertIsNone(mona['workspace'])
        self.assertEqual(octocat['login'], 'octocat')   # Helm entries borrow the owner record's login

    def test_remove_never_touches_an_existing_workspace(self):
        self.add()
        self.service.create('456', 'Mona')
        for _ in range(4):
            self.service.tick()
        before = self.service.get('456')
        self.assertEqual(self.console.remove('456', 'alice'), {'removed': True})
        self.assertEqual(self.console.remove('456', 'alice'), {'removed': False})
        self.assertEqual(self.service.get('456'), before)
        self.assertEqual(before['stage'], 'ready')
        self.service.dex.delete.assert_not_called()
        self.assertIn('slug-mona', self.service.store.records)


class EligibilityTest(unittest.TestCase):
    """The acceptance criteria: no restart to add, immediate effect to remove."""

    def setUp(self):
        self.service = fixture()
        self.service.backend.lookup.side_effect = github_lookup

    def replica(self, clock):
        from provisioning import Provisioning
        service = Provisioning(self.service.store, self.service.dex, self.service.backend,
                               'example.test', Allowlist(self.service.store, {'123'}, clock=clock))
        return service

    def test_console_add_enables_signup_on_a_running_service(self):
        with self.assertRaises(SignupError) as raised:
            self.service.create('456', 'Mona')
        self.assertEqual(raised.exception.code, 'not_eligible')
        AllowlistConsole(self.service).add({'login': 'mona', 'githubId': '456'}, 'alice')
        self.assertEqual(self.service.create('456', 'Mona')['stage'], 'identity')

    def test_removal_blocks_creation_at_once_on_the_other_replica(self):
        clock = Clock()
        first, second = self.replica(clock), self.replica(clock)
        AllowlistConsole(first).add({'login': 'mona', 'githubId': '456'}, 'alice')
        self.assertTrue(second.allowlist.allows('456'))     # second replica's cache is warm
        AllowlistConsole(first).remove('456', 'alice')
        self.assertTrue(second.allowlist.allows('456'))      # the signup page may lag ...
        with self.assertRaises(SignupError) as raised:
            second.create('456', 'Mona')                     # ... but creation never does
        self.assertEqual(raised.exception.code, 'not_eligible')
        self.assertFalse(second.allowlist.allows('456'))     # and that read refreshed the cache
        self.assertNotIn('owner-456', self.service.store.records)

    def test_the_signup_page_catches_up_within_the_cache_window(self):
        clock = Clock()
        first, second = self.replica(clock), self.replica(clock)
        self.assertFalse(second.allowlist.allows('456'))
        AllowlistConsole(first).add({'login': 'mona', 'githubId': '456'}, 'alice')
        clock.now += signup_allowlist.CACHE_SECONDS
        self.assertTrue(second.allowlist.allows('456'))


class StoreRoundTripTest(unittest.TestCase):
    def test_kubestore_save_conflict_reaches_the_retry_loop(self):
        from provision_store import KubeStore
        from unittest.mock import Mock
        store = KubeStore('state')
        existing = {'metadata': {'resourceVersion': '7'}, 'data': {'record': '{"entries": {}}'}}
        store.request = Mock(side_effect=[existing, Conflict(), existing,
                                          {'metadata': {'resourceVersion': '9'}}])
        _, created = Allowlist(store, ()).add('456', 'Mona', 'alice')
        self.assertTrue(created)
        put = store.request.call_args_list[-1]
        self.assertEqual(put.args[:2], ('PUT', '/api/v1/namespaces/state/configmaps/signup-allowlist'))
        self.assertEqual(put.args[2]['metadata']['resourceVersion'], '7')


if __name__ == '__main__':
    unittest.main()
