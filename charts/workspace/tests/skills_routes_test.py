"""Tests for the skills route table that replaced the elif branches (#100).

The first migrated domain with routes on two verbs, so the table is also
where the do_GET / do_POST split now lives. Two layers, mirroring
tests/routing_test.py:
  * The table — `/api/skills/stats` ahead of the detail route, the two POST
    routes that used to sit 70 lines apart in do_POST, and the deliberately
    different name charsets on the GET detail and POST sync routes.
  * End-to-end over a real server — a 401 proves the route matched at all,
    and a name outside the sync route's charset still 404s.

The sync handler's own decision logic (targets, conflicts, force) is covered
by tests/skills_sync_endpoint_test.py and is unchanged by the split.

Run with:
    cd charts/workspace && python3 -m unittest tests.skills_routes_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import skills  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, args_for,
)


class SkillsRouteOrderTests(DomainRouteTests, unittest.TestCase):

    table = skills.ROUTES
    foreign_paths = (('GET', '/api/skills/'), ('GET', '/api/skills/a/b'),
                     ('GET', '/api/skillsx'), ('POST', '/api/skills/x/scan'))
    oauth_samples = (('GET', '/api/skills'), ('GET', '/api/skills/stats'),
                     ('GET', '/api/skills/thing'), ('POST', '/api/skills/_scan'),
                     ('POST', '/api/skills/thing/sync'))
    wrong_verb_samples = (('POST', '/api/skills'), ('POST', '/api/skills/stats'),
                          ('GET', '/api/skills/x/sync'))
    query_handlers = {'handle_skills_list'}

    def test_stats_is_not_read_as_a_skill_name(self):
        # The hazard: `stats` matches the detail pattern, so the stats route
        # only works while it stays registered first.
        self.assertEqual(self.resolve('GET', '/api/skills/stats'),
                         'handle_skills_stats')

    def test_list_and_detail_routes(self):
        self.assertEqual(self.resolve('GET', '/api/skills'),
                         'handle_skills_list')
        self.assertEqual(self.resolve('GET', '/api/skills/my.skill_v2'),
                         'handle_skills_get')
        self.assertEqual(args_for(skills.ROUTES, 'GET', '/api/skills/my.skill_v2'),
                         ('my.skill_v2',))

    def test_the_two_post_routes(self):
        # Adjacent in the table; 70 lines apart in the chain they replace.
        self.assertEqual(self.resolve('POST', '/api/skills/_scan'),
                         'handle_skills_scan')
        self.assertEqual(self.resolve('POST', '/api/skills/my-skill/sync'),
                         'handle_skills_sync')
        self.assertEqual(
            args_for(skills.ROUTES, 'POST', '/api/skills/my-skill/sync'),
            ('my-skill',))

    def test_scan_on_get_is_the_detail_route(self):
        # GET /api/skills/_scan has always been the detail route — `_scan` is
        # a legal skill name to that pattern. Captured, not changed. (The
        # generic verb check covers the rest of this domain's verb pairs.)
        self.assertEqual(self.resolve('GET', '/api/skills/_scan'),
                         'handle_skills_get')

    def test_sync_takes_a_stricter_name_charset_than_the_detail_route(self):
        # Only the filesystem-safe set may ever build a write path.
        self.assertEqual(self.resolve('GET', '/api/skills/My_Skill'),
                         'handle_skills_get')
        self.assertIsNone(self.resolve('POST', '/api/skills/My_Skill/sync'))


class SkillsEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    oauth_reachable = (('GET', '/api/skills'), ('POST', '/api/skills/_scan'))
    # The sync route's charset is stricter than the detail route's, so an
    # upper-case name never reaches the handler.
    unmatched_shapes = (('GET', '/api/skills/a/b'),
                        ('POST', '/api/skills/My_Skill/sync'),
                        ('POST', '/api/skills'))

    def test_reads_are_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/skills', '/api/skills?system=claude',
                     '/api/skills/stats', '/api/skills/thing'):
            self.assertEqual(self.get(path)[0], 401, path)

    def test_writes_are_auth_gated(self):
        for path in ('/api/skills/_scan', '/api/skills/my-skill/sync'):
            self.assertEqual(self.post(path)[0], 401, path)


if __name__ == '__main__':
    unittest.main()
