"""Deterministic connector engine — fetch, paginate, map, act.

This is the correctness core of the Board Processor. The tests that matter most
are in `CompleteSemanticsTests`: a connector that reports success on a partial
board is worse than no connector, and two real vendors make that a live hazard
(Jira silently truncates; GitLab omits pagination headers entirely under keyset
pagination).

Run:  python3 -m unittest tests.boards_engine_test   (from charts/workspace/)
"""

import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from boards import engine  # noqa: E402
from boards import schema  # noqa: E402
from tests import board_fixtures as fx  # noqa: E402
from tests.board_fixtures import FakeHTTP  # noqa: E402


def J(obj, status=200, headers=None):
    return (status, headers or {}, json.dumps(obj).encode('utf-8'))


def page_of(n, start=0):
    return [{'id': start + i, 'key': f'SUP-{start + i}',
             'fields': {'summary': f'ticket {start + i}',
                        'status': {'name': 'To Do'}}} for i in range(n)]


class PathTests(unittest.TestCase):
    def test_dotted_path(self):
        self.assertEqual(engine.get_path({'a': {'b': {'c': 7}}}, 'a.b.c'), 7)

    def test_list_index_segment(self):
        self.assertEqual(engine.get_path({'a': [{'b': 1}, {'b': 2}]}, 'a.1.b'), 2)

    def test_empty_path_returns_root(self):
        obj = [1, 2, 3]
        self.assertIs(engine.get_path(obj, ''), obj)

    def test_missing_segment_returns_default(self):
        self.assertIsNone(engine.get_path({'a': 1}, 'a.b.c'))
        self.assertEqual(engine.get_path({}, 'x', 'fallback'), 'fallback')

    def test_set_path_creates_intermediates(self):
        obj = {}
        engine.set_path(obj, 'variables.after', 'cur-1')
        self.assertEqual(obj, {'variables': {'after': 'cur-1'}})


class InterpolationTests(unittest.TestCase):
    def test_whole_string_token_preserves_type(self):
        out = engine.interpolate('${tr.id}', {'tr': {'id': 31}})
        self.assertEqual(out, 31)
        self.assertIsInstance(out, int)

    def test_embedded_token_stringifies(self):
        out = engine.interpolate('/issue/${item.key}/x', {'item': {'key': 'SUP-5'}})
        self.assertEqual(out, '/issue/SUP-5/x')

    def test_unresolved_token_raises_rather_than_rendering_None(self):
        """A silently malformed request to someone else's board is worse than
        a loud failure."""
        with self.assertRaises(engine.BoardError) as cm:
            engine.interpolate('/issue/${item.ref.missing}', {'item': {'ref': {}}})
        self.assertIn('unresolved interpolation token', str(cm.exception))

    def test_nested_structures_interpolated(self):
        out = engine.interpolate(
            {'a': ['${x}', {'b': '${y}'}]}, {'x': 1, 'y': 'two'})
        self.assertEqual(out, {'a': [1, {'b': 'two'}]})


class AuthTests(unittest.TestCase):
    def test_bearer(self):
        self.assertEqual(engine.auth_headers(fx.GITHUB, 'tok'),
                         {'Authorization': 'Bearer tok'})

    def test_header_template(self):
        self.assertEqual(engine.auth_headers(fx.JIRA, 'abc123'),
                         {'Authorization': 'Basic abc123'})

    def test_no_credential_sends_no_auth_header(self):
        self.assertEqual(engine.auth_headers(fx.JIRA, ''), {})


# ── pagination: all five kinds ─────────────────────────────────────────────

class PaginationKindTests(unittest.TestCase):
    def test_page_token_walks_three_pages(self):
        http = FakeHTTP([
            J({'issues': page_of(50, 0), 'nextPageToken': 't1'}),
            J({'issues': page_of(50, 50), 'nextPageToken': 't2'}),
            J({'issues': page_of(3, 100)}),          # short page, no token
        ])
        out = engine.fetch_items(fx.JIRA, http)
        self.assertEqual(out['pages_fetched'], 3)
        self.assertEqual(out['raw_count'], 103)
        self.assertTrue(out['complete'])
        self.assertIn('nextPageToken=t1', http.calls[1]['url'])
        self.assertIn('nextPageToken=t2', http.calls[2]['url'])

    def test_link_header_walks_and_uses_the_absolute_next_url(self):
        nxt = 'https://api.github.com/repositories/1/issues?page=2'
        http = FakeHTTP([
            J([{'id': i, 'number': i, 'state': 'open'} for i in range(50)],
              headers={'Link': f'<{nxt}>; rel="next", <https://x>; rel="last"'}),
            J([{'id': 99, 'number': 99, 'state': 'open'}]),
        ])
        out = engine.fetch_items(fx.GITHUB, http)
        self.assertEqual(out['pages_fetched'], 2)
        self.assertTrue(out['complete'])
        self.assertEqual(http.calls[1]['url'], nxt)

    def test_cursor_kind_injects_into_the_request_body(self):
        """GraphQL: the URL never changes; the cursor goes in the body."""
        http = FakeHTTP([
            J({'data': {'issues': {
                'nodes': [{'id': f'i{i}', 'identifier': f'ENG-{i}'}
                          for i in range(50)],
                'pageInfo': {'hasNextPage': True, 'endCursor': 'cur-1'}}}}),
            J({'data': {'issues': {
                'nodes': [{'id': 'i50', 'identifier': 'ENG-50'}],
                'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}),
        ])
        out = engine.fetch_items(fx.LINEAR, http)
        self.assertEqual(out['raw_count'], 51)
        self.assertTrue(out['complete'])
        self.assertEqual(http.calls[1]['url'], 'https://api.linear.app/graphql')
        body = json.loads(http.calls[1]['body'].decode())
        self.assertEqual(body['variables']['after'], 'cur-1')
        self.assertIn('query Issues', body['query'])

    def test_next_url_kind(self):
        cfg = _zendesk()
        http = FakeHTTP([
            J({'tickets': [{'id': 1}], 'next_page': 'https://acme.zendesk.com/p2'}),
            J({'tickets': [{'id': 2}], 'next_page': None}),
        ])
        out = engine.fetch_items(cfg, http)
        self.assertEqual(out['raw_count'], 2)
        self.assertTrue(out['complete'])
        self.assertEqual(http.calls[1]['url'], 'https://acme.zendesk.com/p2')

    def test_offset_kind_advances_and_stops_on_short_page(self):
        cfg = _offset_board()
        http = FakeHTTP([
            J({'data': page_of(50, 0), 'total': 60}),
            J({'data': page_of(10, 50), 'total': 60}),
        ])
        out = engine.fetch_items(cfg, http)
        self.assertEqual(out['raw_count'], 60)
        self.assertTrue(out['complete'])
        self.assertIn('startAt=50', http.calls[1]['url'])
        self.assertIn('maxResults=50', http.calls[1]['url'])

    def test_offset_without_total_uses_the_short_page_terminator(self):
        cfg = _offset_board()
        cfg['list']['pagination'].pop('total_path')
        http = FakeHTTP([
            J({'data': page_of(50, 0)}),
            J({'data': page_of(7, 50)}),
        ])
        out = engine.fetch_items(cfg, http)
        self.assertEqual(out['raw_count'], 57)
        self.assertTrue(out['complete'])


# ── the correctness core ───────────────────────────────────────────────────

class CompleteSemanticsTests(unittest.TestCase):
    def test_short_page_with_no_terminator_is_complete(self):
        http = FakeHTTP([J({'issues': page_of(3)})])
        out = engine.fetch_items(fx.JIRA, http)
        self.assertTrue(out['complete'])
        self.assertEqual(out['truncation_reason'], '')

    def test_full_page_with_no_terminator_is_NOT_complete(self):
        """Jira's silent truncation: a full page and no nextPageToken means we
        cannot tell whether more exist, so we must not claim we can."""
        http = FakeHTTP([J({'issues': page_of(50)})])
        out = engine.fetch_items(fx.JIRA, http)
        self.assertFalse(out['complete'])
        self.assertEqual(out['truncation_reason'],
                         'full_page_no_pagination_metadata')
        self.assertEqual(out['raw_count'], 50)

    def test_gitlab_keyset_missing_headers_is_not_complete(self):
        """GitLab returns NO Link header at all under keyset pagination. A
        connector reading that as 'one page' would truncate a 10,000-issue
        project."""
        cfg = _gitlab()
        http = FakeHTTP([J([{'id': i, 'iid': i, 'project_id': 42}
                            for i in range(50)])])   # no Link header
        out = engine.fetch_items(cfg, http)
        self.assertFalse(out['complete'])
        self.assertEqual(out['truncation_reason'],
                         'full_page_no_pagination_metadata')

    def test_link_header_without_next_is_a_POSITIVE_terminator(self):
        """A FULL last page whose Link header carries no `next`.

        GitHub keeps sending rel="prev"/rel="first" on the final page, so the
        absence of `next` there is the vendor saying "this is the end" — not
        missing metadata. Treating it as absent made a correct walk report
        complete=False whenever the item count was an exact multiple of
        page_size, because the last page is then full and the short-page
        fallback cannot rescue it. Found against a real 6-issue repo at
        per_page=2."""
        nxt = 'https://api.github.com/repositories/1/issues?page=2'
        http = FakeHTTP([
            J([{'id': i, 'number': i, 'state': 'open'} for i in range(50)],
              headers={'Link': f'<{nxt}>; rel="next", <https://x>; rel="last"'}),
            J([{'id': i, 'number': i, 'state': 'open'} for i in range(50, 100)],
              headers={'Link': '<https://x?page=1>; rel="prev", '
                               '<https://x?page=1>; rel="first"'}),
        ])
        out = engine.fetch_items(fx.GITHUB, http)
        self.assertEqual(out['pages_fetched'], 2)
        self.assertEqual(out['raw_count'], 100)
        self.assertTrue(out['complete'])
        self.assertEqual(out['truncation_reason'], '')

    def test_link_header_ENTIRELY_absent_is_still_not_complete(self):
        """The other side of the same coin: no Link header at all is genuinely
        unknowable, and must not be softened by the fix above."""
        http = FakeHTTP([
            J([{'id': i, 'number': i, 'state': 'open'} for i in range(50)]),
        ])
        out = engine.fetch_items(fx.GITHUB, http)
        self.assertFalse(out['complete'])
        self.assertEqual(out['truncation_reason'],
                         'full_page_no_pagination_metadata')

    def test_max_pages_reached_is_not_complete(self):
        http = FakeHTTP([J({'issues': page_of(50, i * 50), 'nextPageToken': f't{i}'})
                         for i in range(4)])
        out = engine.fetch_items(fx.JIRA, http, max_pages=3)
        self.assertFalse(out['complete'])
        self.assertEqual(out['truncation_reason'], 'max_pages')
        self.assertEqual(out['pages_fetched'], 3)

    def test_expired_cursor_reports_partial_honestly(self):
        """A run cannot store a cursor, pause and resume — cursors expire."""
        http = FakeHTTP([
            J({'data': {'issues': {
                'nodes': [{'id': f'i{i}'} for i in range(50)],
                'pageInfo': {'hasNextPage': True, 'endCursor': 'cur-1'}}}}),
            (400, {}, b'{"errors":[{"message":"CursorExpiredError"}]}'),
        ])
        out = engine.fetch_items(fx.LINEAR, http)
        self.assertFalse(out['complete'])
        self.assertEqual(out['truncation_reason'], 'cursor_expired')
        self.assertEqual(out['raw_count'], 50)

    def test_no_cursor_is_ever_persisted_in_the_result(self):
        http = FakeHTTP([
            J({'data': {'issues': {
                'nodes': [{'id': 'i1'}],
                'pageInfo': {'hasNextPage': False, 'endCursor': 'cur-9'}}}}),
        ])
        out = engine.fetch_items(fx.LINEAR, http)
        self.assertNotIn('cur-9', json.dumps(out, default=str))

    def test_first_page_http_error_reports_status_not_completion(self):
        http = FakeHTTP([(401, {}, b'{"message":"Bad credentials"}')])
        out = engine.fetch_items(fx.JIRA, http)
        self.assertFalse(out['complete'])
        self.assertEqual(out['truncation_reason'], 'http_401')
        self.assertIn('Bad credentials', out['error'])

    def test_items_path_not_found_is_reported_not_silently_empty(self):
        http = FakeHTTP([J({'values': page_of(3)})])   # wrong key
        out = engine.fetch_items(fx.JIRA, http)
        self.assertFalse(out['complete'])
        self.assertEqual(out['truncation_reason'], 'items_path_not_found')
        self.assertIn('issues', out['error'])

    def test_non_json_response_raises_a_clear_board_error(self):
        http = FakeHTTP([(200, {}, b'<html>login</html>')])
        with self.assertRaises(engine.BoardError) as cm:
            engine.fetch_items(fx.JIRA, http)
        self.assertIn('not JSON', str(cm.exception))

    def test_every_page_goes_through_the_same_guarded_callable(self):
        """SSRF must apply to the fetch URL AND every pagination next URL."""
        evil = 'http://169.254.169.254/latest/meta-data/'
        http = FakeHTTP([
            J({'tickets': [{'id': 1}], 'next_page': evil}),
            J({'tickets': [{'id': 2}], 'next_page': None}),
        ])
        engine.fetch_items(_zendesk(), http)
        self.assertEqual(http.calls[1]['url'], evil,
                         'the next URL must be handed to the injected (guarded) '
                         'http callable, never fetched by another path')


# ── mapping ────────────────────────────────────────────────────────────────

class MappingTests(unittest.TestCase):
    def test_id_key_and_ref_are_three_different_things(self):
        """GitLab: global id 46, project-scoped iid 5, project 42. Conflating
        them makes processed-markers collide across projects."""
        cfg = _gitlab()
        raw = {'id': 46, 'iid': 5, 'project_id': 42, 'title': 'Bug',
               'state': 'opened'}
        item = engine.map_item(raw, cfg)
        self.assertEqual(item['id'], '46')
        self.assertEqual(item['key'], '5')
        self.assertEqual(item['ref'], {'project': 42, 'iid': 5})

        url = engine.interpolate(
            '${base_url}/projects/${item.ref.project}/issues/${item.ref.iid}',
            {'base_url': cfg['base_url'], 'item': item})
        self.assertEqual(url, 'https://gitlab.com/api/v4/projects/42/issues/5')

    def test_github_state_reason_round_trips_through_raw(self):
        completed = engine.map_item(
            {'id': 1, 'number': 1, 'state': 'closed',
             'state_reason': 'completed'}, fx.GITHUB)
        not_planned = engine.map_item(
            {'id': 2, 'number': 2, 'state': 'closed',
             'state_reason': 'not_planned'}, fx.GITHUB)

        self.assertEqual(completed['status']['normalized'], 'CLOSED')
        self.assertEqual(not_planned['status']['normalized'], 'CLOSED')
        self.assertEqual(completed['status']['raw'], 'closed+completed')
        self.assertEqual(not_planned['status']['raw'], 'closed+not_planned')

    def test_join_skips_a_missing_component(self):
        item = engine.map_item({'id': 3, 'number': 3, 'state': 'open'}, fx.GITHUB)
        self.assertEqual(item['status']['raw'], 'open')
        self.assertEqual(item['status']['normalized'], 'OPEN')

    def test_tags_from_objects_use_the_name_not_a_python_repr(self):
        """GitHub labels are objects; Jira's are plain strings. str()-ing the
        element put `{'id': 11748764427, 'node_id': ...}` in the UI and in
        agent prompts as though it were a tag name. Found mapping real
        GitHub labels."""
        cfg = dict(fx.GITHUB)
        cfg['map'] = dict(cfg['map'], tags='labels')
        item = engine.map_item(
            {'id': 1, 'number': 1, 'state': 'open',
             'labels': [{'id': 11748764427, 'node_id': 'LA_kw', 'name': 'bug',
                         'color': 'd73a4a'},
                        {'id': 2, 'name': 'needs-triage'}]}, cfg)
        self.assertEqual(item['tags'], ['bug', 'needs-triage'])

    def test_tags_from_plain_strings_are_unchanged(self):
        cfg = dict(fx.JIRA)
        cfg['map'] = dict(cfg['map'], tags='fields.labels')
        item = engine.map_item(
            {'id': 1, 'key': 'S-1', 'fields': {'labels': ['billing', 'eu']}}, cfg)
        self.assertEqual(item['tags'], ['billing', 'eu'])

    def test_a_tag_object_with_no_recognisable_name_is_dropped(self):
        """Dropping beats emitting a repr: a wrong tag reads as data."""
        cfg = dict(fx.GITHUB)
        cfg['map'] = dict(cfg['map'], tags='labels')
        item = engine.map_item(
            {'id': 1, 'number': 1, 'state': 'open',
             'labels': [{'colour': 'red'}, {'name': 'kept'}]}, cfg)
        self.assertEqual(item['tags'], ['kept'])

    def test_unmapped_value_passes_through_as_raw(self):
        """Not coerced into a bucket it does not belong in."""
        item = engine.map_item(
            {'id': 9, 'key': 'SUP-9',
             'fields': {'status': {'name': 'Pending Customer'},
                        'priority': {'name': 'Trivial'}}}, fx.JIRA)
        self.assertIsNone(item['status']['normalized'])
        self.assertEqual(item['status']['raw'], 'Pending Customer')
        self.assertIsNone(item['priority']['normalized'])
        self.assertEqual(item['priority']['raw'], 'Trivial')

    def test_enum_match_is_case_insensitive(self):
        item = engine.map_item(
            {'id': 1, 'key': 'S-1', 'fields': {'status': {'name': 'done'}}},
            fx.JIRA)
        self.assertEqual(item['status']['normalized'], 'CLOSED')

    def test_full_raw_object_is_always_retained(self):
        raw = {'id': 5, 'key': 'SUP-5', 'fields': {'summary': 's'},
               'vendor_only_field': {'deep': [1, 2, 3]}}
        item = engine.map_item(raw, fx.JIRA)
        self.assertEqual(item['raw'], raw)
        self.assertEqual(item['raw']['vendor_only_field']['deep'], [1, 2, 3])

    def test_item_with_every_optional_null_maps_without_raising(self):
        raw = {'id': 7, 'key': None, 'fields': {
            'summary': None, 'description': None, 'status': None,
            'priority': None, 'assignee': None, 'reporter': None,
            'project': None, 'labels': None, 'created': None, 'updated': None}}
        item = engine.map_item(raw, fx.JIRA)
        self.assertEqual(item['id'], '7')
        self.assertEqual(item['title'], '')
        self.assertEqual(item['tags'], [])
        self.assertEqual(item['assignee'], {})
        self.assertIsNone(item['status']['normalized'])
        self.assertIsNone(item['status']['raw'])

    def test_missing_id_is_a_hard_error(self):
        with self.assertRaises(engine.BoardError) as cm:
            engine.map_item({'key': 'SUP-1'}, fx.JIRA)
        self.assertIn('processed-markers would collide', str(cm.exception))

    def test_template_field_can_reach_base_url(self):
        item = engine.map_item({'id': 1, 'key': 'SUP-1', 'fields': {}}, fx.JIRA)
        self.assertEqual(item['url'], 'https://acme.atlassian.net/browse/SUP-1')

    def test_display_template_degrades_rather_than_half_building_a_url(self):
        """A missing optional must not produce `.../browse/None`, and must not
        drop an otherwise workable item off the board."""
        item = engine.map_item({'id': 1, 'key': None, 'fields': {}}, fx.JIRA)
        self.assertEqual(item['url'], '')
        self.assertEqual(item['id'], '1')

    def test_request_templates_stay_strict(self):
        """The leniency above is scoped to display fields only — an action URL
        with an unresolvable token must still fail loudly."""
        item = engine.map_item({'id': 46, 'key': 'SUP-5', 'fields': {}}, fx.JIRA)
        item['ref'] = {}                      # issue_key gone
        with self.assertRaises(engine.BoardError):
            engine.run_action(fx.JIRA, item, 'set_status', {'status': 'Done'},
                              FakeHTTP([]), board_id='b1')

    def test_deep_items_path(self):
        self.assertEqual(
            engine.get_path({'data': {'issues': {'nodes': [1, 2]}}},
                            'data.issues.nodes'), [1, 2])

    def test_map_errors_are_collected_not_fatal(self):
        http = FakeHTTP([J({'issues': [{'key': 'no-id'}, {'id': 2, 'key': 'ok'}]})])
        out = engine.fetch_items(fx.JIRA, http)
        self.assertEqual(len(out['items']), 1)
        self.assertEqual(len(out['map_errors']), 1)


class ContentHashTests(unittest.TestCase):
    def _item(self, **over):
        base = {'id': '1', 'title': 't', 'body': 'b',
                'status': {'raw': 'To Do'}, 'priority': {'raw': 'P2'},
                'updated_at': '2026-01-01'}
        base.update(over)
        return base

    def test_stable_for_identical_items(self):
        self.assertEqual(engine.content_hash(self._item()),
                         engine.content_hash(self._item()))

    def test_changes_when_the_body_changes(self):
        self.assertNotEqual(engine.content_hash(self._item()),
                            engine.content_hash(self._item(body='edited')))

    def test_ignores_vendor_churn_outside_the_tracked_fields(self):
        a = self._item()
        b = self._item()
        b['raw'] = {'view_count': 991}
        self.assertEqual(engine.content_hash(a), engine.content_hash(b))

    def test_updated_at_alone_does_NOT_change_the_hash(self):
        """Our OWN comment bumps updated_at. Hashing it made a run invalidate
        the processed markers it had just written, so an immediate re-run
        re-selected exactly the items it had worked. Seen for real: approving
        one staged comment on a GitHub issue put it straight back into the
        next run's selection."""
        self.assertEqual(
            engine.content_hash(self._item()),
            engine.content_hash(self._item(updated_at='2026-06-30T12:00:00Z')))

    def test_a_real_edit_still_changes_the_hash(self):
        """Removing updated_at must not blunt the actual signal."""
        base = self._item()
        for field, value in (('title', 'retitled'), ('body', 'rewritten')):
            self.assertNotEqual(engine.content_hash(base),
                                engine.content_hash(self._item(**{field: value})),
                                f'{field} must still count as a change')
        self.assertNotEqual(
            engine.content_hash(base),
            engine.content_hash(self._item(status={'raw': 'Done'})))
        self.assertNotEqual(
            engine.content_hash(base),
            engine.content_hash(self._item(priority={'raw': 'P1'})))


# ── actions ────────────────────────────────────────────────────────────────

class ActionTests(unittest.TestCase):
    def _jira_item(self):
        return engine.map_item(
            {'id': 46, 'key': 'SUP-5', 'fields': {'summary': 'Refund'}}, fx.JIRA)

    def test_multi_step_select_picks_the_right_transition_by_name(self):
        http = FakeHTTP([
            J({'transitions': [
                {'id': '11', 'to': {'name': 'In Progress'}},
                {'id': '31', 'to': {'name': 'Done'}},
            ]}),
            J({'ok': True}, status=204),
        ])
        out = engine.run_action(fx.JIRA, self._jira_item(), 'set_status',
                                {'status': 'Done'}, http, credential='c',
                                board_id='b1')
        self.assertTrue(out['ok'])
        self.assertEqual(len(http.calls), 2)
        self.assertIn('/issue/SUP-5/transitions', http.calls[0]['url'])
        self.assertEqual(json.loads(http.calls[1]['body'].decode()),
                         {'transition': {'id': '31'}})

    def test_select_no_match_names_the_available_values(self):
        http = FakeHTTP([
            J({'transitions': [{'id': '11', 'to': {'name': 'In Progress'}}]}),
        ])
        with self.assertRaises(engine.BoardError) as cm:
            engine.run_action(fx.JIRA, self._jira_item(), 'set_status',
                              {'status': 'Done'}, http, board_id='b1')
        msg = str(cm.exception)
        self.assertIn('select matched nothing', msg)
        self.assertIn('In Progress', msg)

    def test_undeclared_action_is_refused(self):
        """The action list IS the allowlist."""
        with self.assertRaises(engine.BoardError) as cm:
            engine.run_action(fx.JIRA, self._jira_item(), 'delete_project', {},
                              FakeHTTP([]), board_id='b1')
        self.assertIn('not declared by this connector', str(cm.exception))
        self.assertIn('comment, set_status', str(cm.exception))

    def test_missing_required_param_is_refused_before_any_request(self):
        http = FakeHTTP([])
        with self.assertRaises(engine.BoardError) as cm:
            engine.run_action(fx.JIRA, self._jira_item(), 'comment', {}, http,
                              board_id='b1')
        self.assertIn('missing required parameter', str(cm.exception))
        self.assertEqual(http.calls, [])

    def test_comment_embeds_a_stable_marker(self):
        http = FakeHTTP([J({'comments': []}), J({'id': 1})])
        out = engine.run_action(fx.JIRA, self._jira_item(), 'comment',
                                {'body': 'Hi Dana'}, http, board_id='b1')
        self.assertTrue(out['ok'])
        posted = json.loads(http.calls[1]['body'].decode())['body']
        self.assertIn('Hi Dana', posted)
        self.assertIn('[kc:b1:46:', posted)

    def test_retry_detects_our_own_prior_comment_and_skips(self):
        """Posting a comment twice is a visible mistake in front of a
        customer."""
        item = self._jira_item()
        ahash = engine.action_hash('b1', item['id'], 'comment', {'body': 'Hi'})
        marker = f'[kc:b1:46:{ahash}]'
        http = FakeHTTP([J({'comments': [{'body': f'Hi\n\n{marker}'}]})])
        out = engine.run_action(fx.JIRA, item, 'comment', {'body': 'Hi'}, http,
                                board_id='b1')
        self.assertTrue(out['ok'])
        self.assertEqual(out['skipped'], 'already_applied')
        self.assertEqual(len(http.calls), 1, 'must not POST a second comment')

    def test_a_different_comment_body_is_not_treated_as_already_applied(self):
        item = self._jira_item()
        ahash = engine.action_hash('b1', item['id'], 'comment', {'body': 'Hi'})
        http = FakeHTTP([
            J({'comments': [{'body': f'Hi\n\n[kc:b1:46:{ahash}]'}]}),
            J({'id': 2}),
        ])
        out = engine.run_action(fx.JIRA, item, 'comment',
                                {'body': 'Different'}, http, board_id='b1')
        self.assertTrue(out['ok'])
        self.assertNotIn('skipped', out)
        self.assertEqual(len(http.calls), 2)

    def test_probe_failure_is_surfaced_not_swallowed(self):
        """'We could not check' must not read as 'safe to post'."""
        http = FakeHTTP([(500, {}, b'boom'), J({'id': 3})])
        out = engine.run_action(fx.JIRA, self._jira_item(), 'comment',
                                {'body': 'x'}, http, board_id='b1')
        self.assertTrue(out['ok'])
        self.assertIn('probe failed', out['evidence']['probe_warning'])

    def test_ok_reflects_the_vendor_status_not_optimism(self):
        http = FakeHTTP([
            J({'transitions': [{'id': '31', 'to': {'name': 'Done'}}]}),
            (403, {}, b'{"errorMessages":["no permission"]}'),
        ])
        out = engine.run_action(fx.JIRA, self._jira_item(), 'set_status',
                                {'status': 'Done'}, http, board_id='b1')
        self.assertFalse(out['ok'])
        self.assertIn('403', out['error'])
        self.assertIn('no permission', out['error'])

    def test_graphql_action_sends_the_operation_in_the_body(self):
        item = engine.map_item({'id': 'lin-1', 'identifier': 'ENG-1'}, fx.LINEAR)
        http = FakeHTTP([J({'data': {'commentCreate': {'success': True}}})])
        out = engine.run_action(fx.LINEAR, item, 'comment', {'body': 'hello'},
                                http, credential='k', board_id='b2')
        self.assertTrue(out['ok'])
        body = json.loads(http.calls[0]['body'].decode())
        self.assertEqual(body['variables'], {'id': 'lin-1', 'body': 'hello'})

    def test_write_cost_reads_the_declared_writes(self):
        self.assertEqual(engine.write_cost(fx.JIRA, 'comment'), 1)
        self.assertEqual(engine.write_cost(fx.JIRA, 'nope'), 1)

    def test_action_hash_is_stable_and_parameter_sensitive(self):
        a = engine.action_hash('b', '1', 'comment', {'body': 'x'})
        b = engine.action_hash('b', '1', 'comment', {'body': 'x'})
        c = engine.action_hash('b', '1', 'comment', {'body': 'y'})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


# ── extra connectors used only here ────────────────────────────────────────

def _gitlab():
    """The sharpest test of id/ref separation, and of missing headers."""
    cfg = {
        'vendor': 'gitlab',
        'display_name': 'GitLab issues',
        'base_url': 'https://gitlab.com/api/v4',
        'credential_ref': '@board-creds/GITLAB_TOKEN',
        'auth': {'kind': 'header', 'header': 'PRIVATE-TOKEN',
                 'template': '${credential}'},
        'list': {
            'request': {'method': 'GET',
                        'url': '${base_url}/projects/42/issues'},
            'items_path': '',
            'page_size': 50,
            'pagination': {'kind': 'link_header'},
        },
        'map': {'id': 'id', 'key': 'iid',
                'ref': {'project': 'project_id', 'iid': 'iid'},
                'title': 'title', 'status_raw': 'state'},
        'enums': {'status': {'OPEN': ['opened'], 'CLOSED': ['closed']}},
    }
    cleaned, errors = schema.validate_connector(cfg)
    assert not errors, errors
    return cleaned


def _zendesk():
    cfg = {
        'vendor': 'zendesk',
        'display_name': 'Acme support',
        'base_url': 'https://acme.zendesk.com/api/v2',
        'credential_ref': '@board-creds/ZENDESK_TOKEN',
        'auth': {'kind': 'basic'},
        'list': {
            'request': {'method': 'GET', 'url': '${base_url}/tickets.json'},
            'items_path': 'tickets',
            'page_size': 100,
            'pagination': {'kind': 'next_url', 'next_path': 'next_page'},
        },
        'map': {'id': 'id', 'key': 'id', 'ref': {'id': 'id'},
                'title': 'subject'},
        'limits': {'per_action': {'update': {'max_events': 30,
                                             'window_seconds': 600}},
                   'per_item_writes': 30},
    }
    cleaned, errors = schema.validate_connector(cfg)
    assert not errors, errors
    return cleaned


def _offset_board():
    cfg = {
        'vendor': 'asana',
        'display_name': 'Asana tasks',
        'base_url': 'https://app.asana.com/api/1.0',
        'credential_ref': '@board-creds/ASANA_TOKEN',
        'auth': {'kind': 'bearer'},
        'list': {
            'request': {'method': 'GET', 'url': '${base_url}/tasks'},
            'items_path': 'data',
            'page_size': 50,
            'pagination': {'kind': 'offset', 'offset_param': 'startAt',
                           'limit_param': 'maxResults', 'total_path': 'total'},
        },
        'map': {'id': 'id', 'key': 'key', 'title': 'fields.summary'},
    }
    cleaned, errors = schema.validate_connector(cfg)
    assert not errors, errors
    return cleaned


if __name__ == '__main__':
    unittest.main()
