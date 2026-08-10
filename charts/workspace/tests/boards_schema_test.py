"""Connector schema validation (#588/#589 Phase 1).

The headline test is `RealConnectorTests`: the three boards that disagree about
everything must all validate against ONE schema. If that ever fails, the honest
response is to narrow the epic's scope to REST ticketing — NOT to add a
code-execution escape hatch.

Run:  python3 -m unittest tests.boards_schema_test   (from charts/workspace/)
"""

import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from boards import schema  # noqa: E402
from tests import board_fixtures as fx  # noqa: E402


class RealConnectorTests(unittest.TestCase):
    """One schema, three incompatible boards."""

    def test_all_three_real_connectors_validate(self):
        for name, cfg in fx.ALL.items():
            with self.subTest(vendor=name):
                cleaned, errors = schema.validate_connector(cfg)
                self.assertEqual(errors, [], f'{name}: {errors}')
                self.assertIsNotNone(cleaned)

    def test_cleaned_is_a_fresh_allowlisted_dict(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['created_at'] = '2026-01-01T00:00:00Z'
        cleaned, errors = schema.validate_connector(cfg)
        self.assertEqual(errors, [])
        self.assertIsNot(cleaned, cfg)
        self.assertEqual(cleaned['version'], 1)
        self.assertEqual(cleaned['created_at'], '2026-01-01T00:00:00Z')

    def test_base_url_trailing_slash_normalized(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['base_url'] = 'https://acme.atlassian.net/'
        cleaned, errors = schema.validate_connector(cfg)
        self.assertEqual(errors, [])
        self.assertEqual(cleaned['base_url'], 'https://acme.atlassian.net')

    def test_action_names_is_the_allowlist(self):
        self.assertEqual(schema.action_names(fx.JIRA), ['comment', 'set_status'])
        self.assertEqual(schema.action_names({}), [])


class RequiredFieldTests(unittest.TestCase):
    def _err(self, mutate, vendor='jira'):
        cfg = copy.deepcopy(fx.ALL[vendor])
        mutate(cfg)
        cleaned, errors = schema.validate_connector(cfg)
        self.assertIsNone(cleaned)
        return ' | '.join(errors)

    def test_not_an_object(self):
        cleaned, errors = schema.validate_connector('nope')
        self.assertIsNone(cleaned)
        self.assertIn('must be an object', errors[0])

    def test_display_name_required(self):
        self.assertIn('display_name', self._err(lambda c: c.pop('display_name')))

    def test_vendor_required(self):
        self.assertIn('vendor', self._err(lambda c: c.pop('vendor')))

    def test_base_url_must_be_absolute(self):
        self.assertIn('absolute', self._err(
            lambda c: c.__setitem__('base_url', '/rest/api')))

    def test_version_must_be_1(self):
        self.assertIn('version', self._err(lambda c: c.__setitem__('version', 2)))

    def test_unknown_top_level_field_rejected(self):
        self.assertIn('unknown field', self._err(
            lambda c: c.__setitem__('exec', 'rm -rf /')))

    def test_map_id_required(self):
        msg = self._err(lambda c: c['map'].pop('id'))
        self.assertIn('map.id is required', msg)

    def test_unknown_map_field_rejected(self):
        self.assertIn('unknown map field', self._err(
            lambda c: c['map'].__setitem__('severity', 'fields.severity')))

    def test_list_items_path_must_be_present(self):
        self.assertIn('items_path', self._err(lambda c: c['list'].pop('items_path')))

    def test_empty_items_path_is_legal(self):
        """GitHub's issues endpoint returns a top-level array."""
        cleaned, errors = schema.validate_connector(copy.deepcopy(fx.GITHUB))
        self.assertEqual(errors, [])
        self.assertEqual(cleaned['list']['items_path'], '')

    def test_all_errors_reported_not_just_the_first(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg.pop('display_name')
        cfg.pop('vendor')
        cfg['map'].pop('id')
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertGreaterEqual(len(errors), 3)


class CredentialRefTests(unittest.TestCase):
    def test_workspace_github_ref_ok(self):
        ok, err = schema.validate_credential_ref('@workspace-github')
        self.assertTrue(ok, err)

    def test_provider_keys_ref_ok(self):
        ok, err = schema.validate_credential_ref('@board-creds/JIRA_API_TOKEN')
        self.assertTrue(ok, err)

    def test_empty_ref_ok_for_a_public_board(self):
        ok, _err = schema.validate_credential_ref('')
        self.assertTrue(ok)

    def test_inline_secret_rejected(self):
        """A connector renders in the UI and gets pasted into issues."""
        ok, err = schema.validate_credential_ref('ghp_ACTUALSECRETVALUE123456')
        self.assertFalse(ok)
        self.assertIn('never an inline secret', err)

    def test_unknown_reference_namespace_rejected(self):
        ok, err = schema.validate_credential_ref('@vault/prod/token')
        self.assertFalse(ok)
        self.assertIn('unknown credential reference', err)

    def test_lowercase_provider_key_rejected(self):
        ok, _err = schema.validate_credential_ref('@board-creds/jira_token')
        self.assertFalse(ok)

    def test_auth_kind_requires_a_credential_ref(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['credential_ref'] = ''
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertTrue(any('requires a credential_ref' in e for e in errors), errors)


class InterpolationTokenTests(unittest.TestCase):
    """The closed token set is what stops a connector exfiltrating pod state."""

    def test_env_token_rejected_in_a_list_url(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['list']['request']['url'] = '${base_url}/x?leak=${env.ANTHROPIC_API_KEY}'
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertTrue(any('${env}' in e for e in errors), errors)

    def test_credential_token_rejected_outside_auth_template(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['list']['request']['query']['t'] = '${credential}'
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertTrue(any('${credential}' in e for e in errors), errors)

    def test_credential_token_allowed_in_auth_template(self):
        _cleaned, errors = schema.validate_connector(copy.deepcopy(fx.JIRA))
        self.assertEqual(errors, [])

    def test_params_token_rejected_in_the_list_request(self):
        """`params` only exists for actions; a list request has no params."""
        cfg = copy.deepcopy(fx.JIRA)
        cfg['list']['request']['query']['jql'] = '${params.jql}'
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertTrue(any('${params}' in e for e in errors), errors)

    def test_tokens_in_finds_nested_tokens(self):
        found = schema.tokens_in({'a': ['${item.ref.id}', {'b': '${params.x}'}]})
        roots = sorted(r for r, _ in found)
        self.assertEqual(roots, ['item', 'params'])


class PaginationTests(unittest.TestCase):
    def _pg_err(self, pagination):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['list']['pagination'] = pagination
        _cleaned, errors = schema.validate_connector(cfg)
        return ' | '.join(errors)

    def test_unknown_kind_rejected(self):
        self.assertIn('kind must be one of',
                      self._pg_err({'kind': 'magic', 'token_path': 'x'}))

    def test_missing_required_field_rejected(self):
        self.assertIn('token_path', self._pg_err(
            {'kind': 'page_token', 'into': {'query': 'p'}}))

    def test_unknown_field_for_kind_rejected(self):
        """A typo'd token_path must not silently disable pagination."""
        msg = self._pg_err({'kind': 'link_header', 'token_path': 'nextPageToken'})
        self.assertIn('unknown field', msg)

    def test_into_must_name_query_or_body(self):
        msg = self._pg_err({'kind': 'cursor', 'cursor_path': 'c',
                            'into': {'header': 'X-Cursor'}})
        self.assertIn('query', msg)

    def test_into_must_have_exactly_one_slot(self):
        msg = self._pg_err({'kind': 'cursor', 'cursor_path': 'c',
                            'into': {'query': 'a', 'body': 'b'}})
        self.assertIn('exactly one', msg)

    def test_all_five_kinds_are_expressible(self):
        valid = [
            {'kind': 'page_token', 'token_path': 'nextPageToken',
             'into': {'query': 'nextPageToken'}},
            {'kind': 'cursor', 'cursor_path': 'd.pageInfo.endCursor',
             'has_more_path': 'd.pageInfo.hasNextPage',
             'into': {'body': 'variables.after'}},
            {'kind': 'next_url', 'next_path': 'next_page'},
            {'kind': 'link_header', 'rel': 'next'},
            {'kind': 'offset', 'offset_param': 'startAt',
             'limit_param': 'maxResults', 'total_path': 'total'},
        ]
        for pg in valid:
            with self.subTest(kind=pg['kind']):
                self.assertEqual(self._pg_err(pg), '')


class EnumTests(unittest.TestCase):
    def test_status_bucket_outside_closed_set_rejected(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['enums']['status']['WONTFIX'] = ['Wont Fix']
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertTrue(any('WONTFIX' in e for e in errors), errors)

    def test_unknown_enum_field_rejected(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['enums']['severity'] = {'HIGH': ['sev1']}
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertTrue(any('unknown enum field' in e for e in errors), errors)

    def test_raw_list_must_be_strings(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['enums']['priority']['HIGH'] = [2]
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertTrue(any('list of raw vendor strings' in e for e in errors), errors)


class FieldSpecTests(unittest.TestCase):
    def _map_err(self, field, spec):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['map'][field] = spec
        _cleaned, errors = schema.validate_connector(cfg)
        return ' | '.join(errors)

    def test_join_composite_accepted(self):
        self.assertEqual(
            self._map_err('status_raw', {'join': ['state', 'state_reason'],
                                         'sep': '+'}), '')

    def test_join_must_be_non_empty(self):
        self.assertIn('non-empty list', self._map_err('status_raw', {'join': []}))

    def test_template_accepted(self):
        self.assertEqual(
            self._map_err('url', {'template': '${base_url}/browse/${item.key}'}), '')

    def test_object_form_needs_template_or_join(self):
        self.assertIn('must have "template" or "join"',
                      self._map_err('title', {'path': 'fields.summary'}))

    def test_unknown_assignee_key_rejected(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['map']['assignee']['manager'] = 'fields.assignee.manager'
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertTrue(any('unknown key' in e for e in errors), errors)

    def test_ref_keys_are_free_form(self):
        """`ref` is whatever the action URLs need to interpolate."""
        cfg = copy.deepcopy(fx.JIRA)
        cfg['map']['ref']['anything_at_all'] = 'id'
        _cleaned, errors = schema.validate_connector(cfg)
        self.assertEqual(errors, [])


class ActionTests(unittest.TestCase):
    def _act_err(self, mutate, vendor='jira'):
        cfg = copy.deepcopy(fx.ALL[vendor])
        mutate(cfg['actions'])
        _cleaned, errors = schema.validate_connector(cfg)
        return ' | '.join(errors)

    def test_steps_required(self):
        self.assertIn('non-empty list',
                      self._act_err(lambda a: a['comment'].__setitem__('steps', [])))

    def test_select_from_must_name_a_prior_step(self):
        def mutate(a):
            a['set_status']['steps'][1]['select']['from'] = 'nope.transitions'
        self.assertIn('not a PRIOR step id', self._act_err(mutate))

    def test_select_cannot_reference_a_later_step(self):
        """Step ids come into scope only for steps that follow them."""
        def mutate(a):
            steps = a['set_status']['steps']
            steps[0], steps[1] = steps[1], steps[0]
        self.assertIn('not a PRIOR step id', self._act_err(mutate))

    def test_alias_token_without_select_is_rejected(self):
        def mutate(a):
            a['set_status']['steps'][1].pop('select')
        self.assertIn('${tr}', self._act_err(mutate))

    def test_duplicate_step_id_rejected(self):
        def mutate(a):
            a['set_status']['steps'][1]['id'] = 't'
        self.assertIn('already used', self._act_err(mutate))

    def test_unknown_step_field_rejected(self):
        def mutate(a):
            a['comment']['steps'][0]['script'] = 'print(1)'
        self.assertIn('unknown field', self._act_err(mutate))

    def test_unknown_action_field_rejected(self):
        self.assertIn('unknown field',
                      self._act_err(lambda a: a['comment'].__setitem__('exec', 'x')))

    def test_bad_action_name_rejected(self):
        self.assertIn('invalid action name',
                      self._act_err(lambda a: a.__setitem__('Delete Everything!', {})))

    def test_too_many_steps_rejected(self):
        def mutate(a):
            a['comment']['steps'] = [dict(a['comment']['steps'][0])
                                     for _ in range(schema.MAX_STEPS_PER_ACTION + 1)]
        self.assertIn('too many steps', self._act_err(mutate))

    def test_idempotency_marker_must_be_a_template(self):
        def mutate(a):
            a['comment']['idempotency']['marker_template'] = 'static-marker'
        self.assertIn('marker_template', self._act_err(mutate))

    def test_idempotency_probe_requires_items_path_and_field(self):
        def mutate(a):
            a['comment']['idempotency'].pop('probe_items_path')
        self.assertIn('probe_items_path', self._act_err(mutate))

    def test_require_actions_flag(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg.pop('actions')
        _cleaned, errors = schema.validate_connector(cfg, require_actions=True)
        self.assertIn('actions is required', errors)
        _cleaned2, errors2 = schema.validate_connector(cfg, require_actions=False)
        self.assertEqual(errors2, [])


class LimitTests(unittest.TestCase):
    def _lim_err(self, limits):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['limits'] = limits
        _cleaned, errors = schema.validate_connector(cfg)
        return ' | '.join(errors)

    def test_global_bucket_validated(self):
        self.assertIn('max_events', self._lim_err({'global': {'window_seconds': 60}}))

    def test_negative_window_rejected(self):
        self.assertIn('positive', self._lim_err(
            {'global': {'max_events': 10, 'window_seconds': -1}}))

    def test_per_item_writes_must_be_positive(self):
        self.assertIn('per_item_writes', self._lim_err({'per_item_writes': 0}))

    def test_limit_detect_shape(self):
        self.assertEqual(self._lim_err({
            'limit_detect': {'statuses': [403, 429],
                             'body_contains': ['secondary rate limit']}}), '')
        self.assertIn('statuses', self._lim_err(
            {'limit_detect': {'statuses': ['429']}}))

    def test_unknown_limit_field_rejected(self):
        self.assertIn('unknown field', self._lim_err({'requests_per_minute': 60}))


class PublicViewTests(unittest.TestCase):
    def test_public_view_reports_credential_set_without_the_value(self):
        cleaned, _errors = schema.validate_connector(copy.deepcopy(fx.JIRA))
        view = schema.public_view(cleaned)
        self.assertTrue(view['credential_set'])
        self.assertEqual(view['credential_ref'], '@board-creds/JIRA_API_TOKEN')
        self.assertNotIn('credential', view)

    def test_public_view_of_a_credential_free_board(self):
        view = schema.public_view({'credential_ref': ''})
        self.assertFalse(view['credential_set'])


if __name__ == '__main__':
    unittest.main()
