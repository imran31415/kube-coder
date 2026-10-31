"""Triggers that start Board runs (#709 part 2).

A cron or webhook with `action: board_run` starts a run on a saved board
instead of a Build. What has to be true, and what each class below pins:

- The config is validated when it is SAVED (`SaveTests`): a typo'd select key,
  a board that does not exist, or a request for autonomous mode is an error on
  the form, not a nightly run that works the whole board.
- A fire starts a PROPOSE-mode run whose origin is the trigger kind and whose
  `trigger` names the trigger (`CronFireTests`, `WebhookFireTests`). The
  origin is coerced to `manual` when unknown, so this is asserted, not assumed.
- A webhook starts work only for the deliveries it asked for, and only for
  the one issue the delivery is about (`WebhookFireTests`).
- A board_run webhook is never open, even with KC_ALLOW_UNSIGNED_WEBHOOKS=1
  (`UnsignedTests`).
- A run that does not start answers an error AND posts a waiting Feed item
  (`FailureTests`).
- A Build trigger behaves exactly as before (`BuildUnchangedTests`).

The real `BoardRunsManager.create` runs throughout, against a temp PVC with
the vendor fetch stubbed — the same seams as tests/boards_runs_api_test.py,
whose base class this reuses.

Run:  python3 -m unittest tests.trigger_board_runs_test   (from charts/workspace/)
"""

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import server  # noqa: E402
from live_state import isolate_feed_and_push, isolate_trigger_runs  # noqa: E402
from tests.boards_runs_api_test import _Base, J, issues  # noqa: E402

WM = server.WebhookManager
CM = server.CronManager
RM = server.BoardRunsManager
TRM = server.TriggerRunsManager
CTM = server.ClaudeTaskManager

SECRET = 'board-run-hmac'


def _kubectl_ok(*args, **kwargs):
    return mock.Mock(returncode=0, stdout='', stderr='')


def _labeled(issue_id=2, label='ai', action='labeled'):
    """The shape of a GitHub `issues` delivery, trimmed to what matters."""
    return {'action': action, 'label': {'name': label},
            'issue': {'id': issue_id, 'number': 40 + issue_id,
                      'title': f'Ticket {issue_id}'}}


class _TriggerBase(_Base):
    def setUp(self):
        super().setUp()
        isolate_feed_and_push(self)
        isolate_trigger_runs(self)
        self.trig_dir = tempfile.mkdtemp(prefix='kctest-trigbr-')
        self.addCleanup(shutil.rmtree, self.trig_dir, True)
        for target, attr, sub in ((WM, 'WEBHOOKS_DIR', 'webhooks'),
                                  (CM, 'CRONS_DIR', 'crons')):
            path = os.path.join(self.trig_dir, sub)
            os.makedirs(path)
            p = mock.patch.object(target, attr, path)
            p.start()
            self.addCleanup(p.stop)
        for attr, value in (('detect_user', 'octo'), ('detect_namespace', 'coder')):
            p = mock.patch.object(CM, attr, staticmethod(lambda v=value: v))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(WM, 'REPLAY_CACHE',
                              server._ReplayCache(capacity=64, ttl_seconds=300))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch('server.subprocess.run', side_effect=_kubectl_ok)
        p.start()
        self.addCleanup(p.stop)
        self.board = self._board('acme-jira')

    # ── fixtures ───────────────────────────────────────────────────────────

    def _cron(self, cron_id='nightly-ai', **over):
        data = {'id': cron_id, 'schedule': '0 2 * * *', 'action': 'board_run',
                'board_run': {'board_id': 'acme-jira',
                              'select': {'tags': [], 'limit': 5},
                              'concurrency': 2}}
        data.update(over)
        cfg, err = CM.create_or_update(data)
        self.assertIsNone(err, err)
        return CM.get_cron(cron_id, include_secrets=True)

    def _webhook(self, webhook_id='gh-ai', **over):
        data = {'id': webhook_id, 'provider': 'github', 'action': 'board_run',
                'hmac_secret': SECRET,
                'board_run': {'board_id': 'acme-jira', 'concurrency': 1},
                'match': {'event': 'issues',
                          'payload': {'action': 'labeled', 'label.name': 'ai'}},
                'item_id_from': 'issue.id'}
        data.update(over)
        cfg, err = WM.create_or_update(data)
        self.assertIsNone(err, err)
        return cfg

    def _write_raw(self, directory, cfg):
        """A hand-written or migrated config: what save-time validation never
        saw, and the fire path must still be safe against."""
        with open(os.path.join(directory, f'{cfg["id"]}.json'), 'w') as f:
            json.dump(cfg, f)

    def _post(self, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else json.dumps(body or {}).encode()
        hdrs = {'Content-Type': 'application/json'}
        hdrs.update(headers or {})
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                   data=data, method='POST', headers=hdrs)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                out = resp.read()
                return resp.status, (json.loads(out) if out else {})
        except urllib.error.HTTPError as e:
            out = e.read()
            try:
                return e.code, json.loads(out)
            except ValueError:
                return e.code, out

    def _deliver(self, webhook_id, payload, event='issues', secret=SECRET):
        raw = json.dumps(payload).encode()
        mac = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self._post(f'/api/webhooks/{webhook_id}', raw=raw, headers={
            'X-Hub-Signature-256': f'sha256={mac}', 'X-GitHub-Event': event})

    def _fire_cron(self, cfg):
        return self._post(f'/api/triggers/cron-fire/{cfg["id"]}', body={},
                          headers={'Authorization': f'Bearer {cfg["fire_token"]}'})

    def _ledger(self, kind, trigger_id):
        return TRM.list_runs(kind, trigger_id)['runs']

    def _waiting_feed(self):
        return [i for i in server.FeedManager.list() if i.get('waiting')]


# ══ 2a: save-time validation ═════════════════════════════════════════════════

class SaveTests(_TriggerBase):
    def test_a_board_run_cron_saves_without_a_prompt(self):
        cfg = self._cron()
        self.assertEqual(cfg['action'], 'board_run')
        self.assertEqual(cfg['board_run']['board_id'], 'acme-jira')
        self.assertEqual(cfg['board_run']['concurrency'], 2)
        # The cleaned select, defaults filled in, is what is stored.
        self.assertEqual(cfg['board_run']['select']['limit'], 5)
        self.assertEqual(cfg['board_run']['select']['order'], 'updated_at asc')

    def test_a_build_trigger_still_needs_its_prompt(self):
        for mgr, data in (
                (CM, {'id': 'c1', 'schedule': '@daily'}),
                (CM, {'id': 'c2', 'schedule': '@daily', 'action': 'build'}),
                (WM, {'id': 'w1'})):
            cfg, err = mgr.create_or_update(data)
            self.assertIsNone(cfg)
            self.assertIn('prompt_template is required', err)

    def test_a_trigger_saved_without_action_is_a_build(self):
        cfg, err = CM.create_or_update({'id': 'old', 'schedule': '@daily',
                                        'prompt_template': 'Summarise'})
        self.assertIsNone(err, err)
        self.assertEqual(cfg['action'], 'build')
        self.assertNotIn('board_run', cfg)

    def test_an_unknown_action_is_refused(self):
        _, err = CM.create_or_update({'id': 'x', 'schedule': '@daily',
                                      'action': 'deploy', 'prompt_template': 'p'})
        self.assertIn('action must be one of', err)

    def test_a_typo_in_select_is_an_error_at_save_time(self):
        for mgr, extra in ((CM, {'schedule': '@daily'}), (WM, {})):
            data = {'id': 'typo', 'action': 'board_run',
                    'board_run': {'board_id': 'acme-jira',
                                  'select': {'tag': ['ai']}}, **extra}
            cfg, err = mgr.create_or_update(data)
            self.assertIsNone(cfg)
            self.assertIn("unknown field 'tag'", err)

    def test_a_board_that_does_not_exist_is_refused(self):
        _, err = CM.create_or_update({
            'id': 'x', 'schedule': '@daily', 'action': 'board_run',
            'board_run': {'board_id': 'no-such-board'}})
        self.assertIn("no board named 'no-such-board'", err)

    def test_asking_for_autonomous_mode_is_refused(self):
        _, err = CM.create_or_update({
            'id': 'x', 'schedule': '@daily', 'action': 'board_run',
            'board_run': {'board_id': 'acme-jira', 'mode': 'autonomous'}})
        self.assertIn('always propose mode', err)

    def test_ignore_processed_is_refused_on_a_trigger(self):
        _, err = CM.create_or_update({
            'id': 'x', 'schedule': '@daily', 'action': 'board_run',
            'board_run': {'board_id': 'acme-jira',
                          'select': {'ignore_processed': True}}})
        self.assertIn('ignore_processed', err)

    def test_concurrency_must_be_a_small_positive_integer(self):
        for bad in (0, 99, True, '2'):
            _, err = CM.create_or_update({
                'id': 'x', 'schedule': '@daily', 'action': 'board_run',
                'board_run': {'board_id': 'acme-jira', 'concurrency': bad}})
            self.assertIn('concurrency', err or '', bad)

    def test_a_board_run_webhook_gets_a_secret_minted(self):
        cfg = self._webhook(hmac_secret=None)
        self.assertTrue(cfg.get('hmac_secret'))

    def test_a_board_run_webhook_whose_secret_was_cleared_cannot_be_saved(self):
        """The update path keeps the prior secret — so a secret-less config on
        disk (hand-written, migrated) stays secret-less, and is refused."""
        self._write_raw(WM.WEBHOOKS_DIR, {'id': 'open', 'prompt_template': 'p'})
        cfg, err = WM.create_or_update(
            {'action': 'board_run', 'board_run': {'board_id': 'acme-jira'}},
            existing_id='open')
        self.assertIsNone(cfg)
        self.assertIn('needs an HMAC secret', err)

    def test_match_is_exact_strings_on_dot_paths_only(self):
        for match, want in (
                ({'payload': {'issue.number': 42}}, 'must be a string'),
                ({'payload': {'labels[0].name': 'ai'}}, 'not a dot-path'),
                ({'event': 'issues', 'when': 'x'}, 'unknown field'),
                ({'event': 'is sues'}, 'event name')):
            _, err = WM.create_or_update({'id': 'm', 'prompt_template': 'p',
                                          'match': match})
            self.assertIn(want, err or '', match)

    def test_item_id_from_only_applies_to_a_board_run(self):
        _, err = WM.create_or_update({'id': 'm', 'prompt_template': 'p',
                                      'item_id_from': 'issue.id'})
        self.assertIn('only applies to a Board run', err)

    def test_the_mcp_list_triggers_tool_shows_the_action(self):
        """`_t_list_triggers` returns the API's lists verbatim (#709 triage),
        so `action` reaches the agent in Chat with no MCP change."""
        import mcp_dashboard
        self._cron()
        self._webhook()
        replies = {'/api/webhooks': {'webhooks': WM.list_webhooks()},
                   '/api/crons': {'crons': CM.list_crons()}}
        with mock.patch.object(mcp_dashboard, '_api',
                               lambda method, path, *a, **k: (200, replies[path])):
            out = json.dumps(mcp_dashboard._t_list_triggers({}))
        self.assertEqual(out.count('\\"action\\": \\"board_run\\"'), 2, out)
        self.assertNotIn(SECRET, out)


# ══ 2b: a cron starts a propose-mode run that names the cron ═════════════════

class CronFireTests(_TriggerBase):
    def test_a_cron_starts_a_propose_run_that_names_the_cron(self):
        cfg = self._cron()
        self._listing(3)
        status, body = self._fire_cron(cfg)
        self.assertEqual(status, 202, body)
        run = RM.get(body['run_id'])
        self.assertEqual(run['origin'], 'cron')
        self.assertEqual(run['trigger'], {'type': 'cron', 'id': 'nightly-ai'})
        self.assertEqual(run['mode'], 'propose')
        self.assertEqual(len(run['items']), 3)
        # The Runs tab reads the list summary, so the label must be there too.
        (summary,) = RM.list_runs('acme-jira')
        self.assertEqual(summary['origin'], 'cron')
        self.assertEqual(summary['trigger']['id'], 'nightly-ai')
        # A run, not a Build: the fire itself launched nothing.
        self.assertEqual(self.tasks.created, [])

    def test_the_fire_is_in_the_ledger_and_the_feed(self):
        cfg = self._cron()
        self._listing(1)
        _, body = self._fire_cron(cfg)
        (entry,) = self._ledger('cron', 'nightly-ai')
        self.assertEqual(entry['outcome'], 'spawned')
        self.assertEqual(entry['run_id'], body['run_id'])
        self.assertEqual(entry['board_id'], 'acme-jira')
        titles = [i['title'] for i in server.FeedManager.list()]
        self.assertIn('Cron trigger fired: nightly-ai', titles)

    def test_a_config_asking_for_autonomous_still_runs_in_propose(self):
        cfg = self._cron()
        cfg['board_run']['mode'] = 'autonomous'
        self._write_raw(CM.CRONS_DIR, cfg)
        self._listing(1)
        status, body = self._fire_cron(cfg)
        self.assertEqual(status, 202, body)
        self.assertEqual(RM.get(body['run_id'])['mode'], 'propose')

    def test_a_suspended_cron_still_starts_nothing(self):
        cfg = self._cron(suspended=True)
        status, _ = self._fire_cron(cfg)
        self.assertEqual(status, 409)
        self.assertEqual(RM.list_runs(), [])


# ══ 2c: only the right deliveries, only that issue ═══════════════════════════

class WebhookFireTests(_TriggerBase):
    def test_a_labeled_ai_delivery_starts_a_run_for_exactly_that_issue(self):
        self._webhook()
        self._listing(3)        # the board lists issues 1, 2, 3
        status, body = self._deliver('gh-ai', _labeled(issue_id=2))
        self.assertEqual(status, 202, body)
        run = RM.get(body['run_id'])
        # GitHub's issue.id is an int; the board keys items by str(id).
        self.assertEqual(list(run['items']), ['2'])
        self.assertEqual(run['select']['item_ids'], ['2'])
        self.assertEqual(run['origin'], 'webhook')
        self.assertEqual(run['trigger'], {'type': 'webhook', 'id': 'gh-ai'})
        self.assertEqual(run['mode'], 'propose')

    def test_other_deliveries_start_nothing(self):
        self._webhook()
        for payload, event, reason in (
                (_labeled(action='opened'), 'issues', 'payload_mismatch'),
                (_labeled(action='edited'), 'issues', 'payload_mismatch'),
                (_labeled(label='bug'), 'issues', 'payload_mismatch'),
                (_labeled(), 'pull_request', 'event_mismatch'),
                ({'zen': 'Keep it logically awesome.', 'hook_id': 1}, 'ping', 'ping')):
            status, body = self._deliver('gh-ai', payload, event=event)
            self.assertEqual((status, body.get('skipped'), body.get('reason')),
                             (200, True, reason), (payload, event))
        self.assertEqual(RM.list_runs(), [])
        outcomes = {e['outcome'] for e in self._ledger('webhook', 'gh-ai')}
        self.assertEqual(outcomes, {'skipped'})

    def test_a_delivery_with_no_item_id_never_works_the_whole_board(self):
        """`select.item_ids: []` would match everything, so a delivery that
        does not carry the id is skipped rather than run unfiltered."""
        self._webhook(match=None)
        status, body = self._deliver('gh-ai', {'action': 'labeled'})
        self.assertEqual((status, body.get('reason')), (200, 'no_item_id'))
        self.assertEqual(RM.list_runs(), [])

    def test_the_test_button_takes_its_event_from_the_body(self):
        self._webhook()
        status, body = self._post('/api/webhooks/gh-ai/test',
                                  body={'payload': _labeled(issue_id=1)})
        self.assertEqual((status, body.get('reason')), (200, 'event_mismatch'))
        self._listing(3)
        status, body = self._post('/api/webhooks/gh-ai/test', body={
            'event': 'issues', 'payload': _labeled(issue_id=1)})
        self.assertEqual(status, 202, body)
        self.assertEqual(list(RM.get(body['run_id'])['items']), ['1'])
        self.assertTrue(self._ledger('webhook', 'gh-ai')[0]['manual'])


class UnsignedTests(_TriggerBase):
    def test_a_board_run_webhook_never_runs_unsigned(self):
        self._write_raw(WM.WEBHOOKS_DIR, {
            'id': 'open', 'action': 'board_run', 'provider': 'github',
            'board_run': {'board_id': 'acme-jira'}})
        with mock.patch.dict(os.environ, {'KC_ALLOW_UNSIGNED_WEBHOOKS': '1'}):
            status, _ = self._post('/api/webhooks/open', body={'n': 1},
                                   headers={'X-GitHub-Event': 'issues'})
        self.assertEqual(status, 404)
        self.assertEqual(RM.list_runs(), [])
        (entry,) = self._ledger('webhook', 'open')
        self.assertEqual(entry['reason'], 'bad_signature')

    def test_the_escape_hatch_still_opens_a_build_webhook(self):
        cfg = {'id': 'open-build', 'prompt_template': 'p'}
        with mock.patch.dict(os.environ, {'KC_ALLOW_UNSIGNED_WEBHOOKS': '1'}):
            self.assertTrue(WM.verify_signature(cfg, b'{}', {}))
            self.assertFalse(WM.verify_signature(
                dict(cfg, action='board_run'), b'{}', {}))


# ══ failures are never silent ════════════════════════════════════════════════

class FailureTests(_TriggerBase):
    def test_a_board_deleted_after_save_answers_an_error_and_a_waiting_item(self):
        cfg = self._cron()
        server.BoardsManager.delete('acme-jira')
        status, body = self._fire_cron(cfg)
        self.assertEqual(status, 502, body)
        self.assertIn('no longer exists', body['error'])
        (item,) = self._waiting_feed()
        self.assertIn("nightly-ai Board run didn't start", item['title'])
        (entry,) = self._ledger('cron', 'nightly-ai')
        self.assertEqual((entry['outcome'], entry['reason']), ('error', 'run_failed'))

    def test_a_full_pod_is_a_429_and_a_waiting_item(self):
        self._webhook()
        with mock.patch.object(CTM, 'count_live_tasks', lambda: 12):
            status, body = self._deliver('gh-ai', _labeled())
        self.assertEqual(status, 429, body)
        self.assertIn('task limit', body['error'])
        (item,) = self._waiting_feed()
        self.assertIn("gh-ai Board run didn't start", item['title'])
        (entry,) = self._ledger('webhook', 'gh-ai')
        self.assertEqual((entry['outcome'], entry['reason']), ('rejected', 'at_capacity'))

    def test_repeated_failures_coalesce_into_one_row(self):
        cfg = self._cron()
        server.BoardsManager.delete('acme-jira')
        self._fire_cron(cfg)
        self._fire_cron(cfg)
        self.assertEqual(len(self._waiting_feed()), 1)

    def test_a_vendor_failure_is_reported_too(self):
        cfg = self._cron()
        self._stub_fetch([J({'errorMessages': ['nope']}, status=401)])
        status, body = self._fire_cron(cfg)
        self.assertEqual(status, 502, body)
        self.assertEqual(len(self._waiting_feed()), 1)
        self.assertEqual(RM.list_runs(), [])


# ══ existing Build triggers ══════════════════════════════════════════════════

class BuildUnchangedTests(_TriggerBase):
    def test_a_build_cron_still_launches_a_build(self):
        CM.create_or_update({'id': 'summary', 'schedule': '@daily',
                             'prompt_template': 'Summarise'})
        cfg = CM.get_cron('summary', include_secrets=True)
        status, body = self._fire_cron(cfg)
        self.assertEqual(status, 202, body)
        self.assertIn('task_id', body)
        self.assertEqual(self.tasks.created[0]['source'], 'cron:summary')
        self.assertEqual(RM.list_runs(), [])

    def test_a_build_webhook_without_match_still_fires_on_ping(self):
        """Skipping GitHub's ping is new behaviour for webhooks that filter or
        start a run; a plain Build webhook keeps doing what it did."""
        WM.create_or_update({'id': 'plain', 'prompt_template': 'Review',
                             'hmac_secret': SECRET})
        status, body = self._deliver('plain', {'zen': 'hi'}, event='ping')
        self.assertEqual(status, 202, body)
        self.assertEqual(len(self.tasks.created), 1)

    def test_a_build_webhook_can_filter_with_match(self):
        WM.create_or_update({'id': 'filtered', 'prompt_template': 'Review',
                             'hmac_secret': SECRET,
                             'match': {'payload': {'action': 'opened'}}})
        status, body = self._deliver('filtered', {'action': 'closed'})
        self.assertEqual((status, body.get('skipped')), (200, True))
        self.assertEqual(self.tasks.created, [])
        status, body = self._deliver('filtered', {'action': 'opened'})
        self.assertEqual(status, 202, body)
        self.assertEqual(len(self.tasks.created), 1)


if __name__ == '__main__':
    unittest.main()
