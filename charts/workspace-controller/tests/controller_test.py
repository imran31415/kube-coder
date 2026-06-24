#!/usr/bin/env python3
"""Unit tests for controller.py — the capacity rollup and the quantity parsers
it builds on. Pure-Python: Prometheus is faked by monkeypatching the query
helpers, so these run with no cluster and no network.

Run from charts/workspace-controller:
    python3 -m unittest discover -s tests -p '*_test.py' -v
"""
import os
import sys
import unittest

# controller.py lives one dir up; import it without installing anything.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import controller  # noqa: E402


class QuantityParsingTest(unittest.TestCase):
    def test_parse_cpu(self):
        self.assertEqual(controller.parse_cpu('2'), 2.0)
        self.assertEqual(controller.parse_cpu('500m'), 0.5)
        self.assertEqual(controller.parse_cpu('3.89'), 3.89)
        self.assertIsNone(controller.parse_cpu(None))
        self.assertIsNone(controller.parse_cpu(''))

    def test_parse_bytes_binary_and_decimal(self):
        self.assertEqual(controller.parse_bytes('1Ki'), 1024)
        self.assertEqual(controller.parse_bytes('1Gi'), 1024 ** 3)
        # 'Gi' must win over 'G' (longest-suffix-first ordering).
        self.assertEqual(controller.parse_bytes('6Gi'), 6 * 1024 ** 3)
        self.assertEqual(controller.parse_bytes('1G'), 1e9)
        self.assertEqual(controller.parse_bytes('1000000'), 1000000)
        self.assertIsNone(controller.parse_bytes(None))


class ResourceBlockTest(unittest.TestCase):
    def test_percentages_of_allocatable(self):
        b = controller._resource_block(allocatable=4.0, workspace=1.0, cluster=2.0)
        self.assertEqual(b['workspacePct'], 25.0)
        self.assertEqual(b['clusterPct'], 50.0)
        self.assertEqual(b['other'], 1.0)  # cluster - workspace

    def test_unknown_allocatable_yields_null_pcts(self):
        b = controller._resource_block(allocatable=None, workspace=1.0, cluster=2.0)
        self.assertIsNone(b['workspacePct'])
        self.assertIsNone(b['clusterPct'])
        self.assertEqual(b['other'], 1.0)

    def test_zero_allocatable_does_not_divide(self):
        b = controller._resource_block(allocatable=0.0, workspace=1.0, cluster=1.0)
        self.assertIsNone(b['workspacePct'])

    def test_other_clamped_when_workspace_exceeds_cluster(self):
        # Scrape skew can momentarily put ws usage above the all-namespace total;
        # `other` must never go negative or the stacked bar breaks.
        b = controller._resource_block(allocatable=4.0, workspace=2.0, cluster=1.5)
        self.assertEqual(b['other'], 0.0)


class NodeRollupTest(unittest.TestCase):
    def _rollup(self):
        return controller._node_rollup(
            alloc_cpu={'n1': 4.0, 'n2': 4.0},
            alloc_mem={'n1': 8e9, 'n2': 8e9},
            alloc_pods={'n1': 110, 'n2': 110},
            ws_cpu={'n1': 1.0, 'n2': 0.5},
            ws_mem={'n1': 2e9, 'n2': 1e9},
            tot_cpu={'n1': 2.0, 'n2': 1.0},
            tot_mem={'n1': 4e9, 'n2': 3e9},
            pods_ws={'n1': 2, 'n2': 1},
            pods_tot={'n1': 10, 'n2': 8},
        )

    def test_cluster_equals_sum_of_nodes(self):
        nodes, cluster = self._rollup()
        self.assertEqual(cluster['nodeCount'], 2)
        self.assertEqual(cluster['cpu']['allocatable'], 8.0)
        self.assertEqual(cluster['cpu']['workspace'], 1.5)
        self.assertEqual(cluster['cpu']['cluster'], 3.0)
        self.assertEqual(cluster['memory']['allocatable'], 16e9)
        self.assertEqual(cluster['pods'], {'allocatable': 220, 'workspace': 3, 'cluster': 18})

    def test_nodes_sorted_by_name(self):
        nodes, _ = self._rollup()
        self.assertEqual([n['name'] for n in nodes], ['n1', 'n2'])

    def test_allocatable_defines_node_set_missing_usage_is_zero(self):
        # n2 has allocatable but no usage series yet (just joined / idle).
        nodes, cluster = controller._node_rollup(
            alloc_cpu={'n1': 4.0, 'n2': 4.0}, alloc_mem={'n1': 8e9, 'n2': 8e9},
            alloc_pods={}, ws_cpu={'n1': 1.0}, ws_mem={}, tot_cpu={'n1': 1.0},
            tot_mem={}, pods_ws={}, pods_tot={},
        )
        self.assertEqual(len(nodes), 2)
        n2 = next(n for n in nodes if n['name'] == 'n2')
        self.assertEqual(n2['cpu']['workspace'], 0.0)
        self.assertEqual(cluster['cpu']['workspace'], 1.0)

    def test_unknown_allocatable_propagates_to_cluster(self):
        # No allocatable series at all -> cluster allocatable is None, not 0,
        # so the UI shows "unknown capacity" rather than "0 cores".
        nodes, cluster = controller._node_rollup(
            alloc_cpu={}, alloc_mem={}, alloc_pods={}, ws_cpu={}, ws_mem={},
            tot_cpu={}, tot_mem={}, pods_ws={}, pods_tot={},
        )
        self.assertEqual(nodes, [])
        self.assertEqual(cluster['nodeCount'], 0)
        self.assertIsNone(cluster['cpu']['allocatable'])


class PerNodeUsageQueryTest(unittest.TestCase):
    def test_join_keys_on_namespace_and_pod(self):
        q = controller._per_node_usage('INNER', 'namespace="coder",pod=~"ws-.*"')
        self.assertIn('sum by (node)', q)
        self.assertIn('on (namespace, pod) group_left (node)', q)
        self.assertIn('kube_pod_info{namespace="coder",pod=~"ws-.*"}', q)
        self.assertIn('INNER', q)


class ClusterCapacityTest(unittest.TestCase):
    """End-to-end shape with Prometheus faked out."""

    def setUp(self):
        self._instant = controller.prom_instant_multi
        self._range = controller.prom_range

    def tearDown(self):
        controller.prom_instant_multi = self._instant
        controller.prom_range = self._range

    def test_happy_path_shape(self):
        def fake_instant(expr):
            if 'kube_node_status_allocatable{resource="cpu"}' in expr:
                return [({'node': 'n1'}, 4.0)]
            if 'kube_node_status_allocatable{resource="memory"}' in expr:
                return [({'node': 'n1'}, 8e9)]
            if 'kube_node_status_allocatable{resource="pods"}' in expr:
                return [({'node': 'n1'}, 110.0)]
            if 'count by (node) (kube_pod_info{namespace' in expr:
                return [({'node': 'n1'}, 2.0)]
            if 'count by (node) (kube_pod_info)' in expr:
                return [({'node': 'n1'}, 9.0)]
            if 'pod=~"ws-.*"' in expr and 'cpu' in expr:
                return [({'node': 'n1'}, 1.0)]
            if 'pod=~"ws-.*"' in expr:
                return [({'node': 'n1'}, 2e9)]
            if 'cpu' in expr:
                return [({'node': 'n1'}, 2.0)]
            return [({'node': 'n1'}, 4e9)]

        controller.prom_instant_multi = fake_instant
        controller.prom_range = lambda expr, s, st: [[1000, 1.0], [1060, 1.5]]

        cap = controller.cluster_capacity(range_seconds=600, step=60)
        self.assertIsNone(cap['metricsError'])
        self.assertEqual(cap['cluster']['nodeCount'], 1)
        self.assertEqual(cap['cluster']['cpu']['allocatable'], 4.0)
        self.assertEqual(cap['cluster']['cpu']['workspace'], 1.0)
        self.assertEqual(cap['cluster']['cpu']['clusterPct'], 50.0)
        self.assertEqual(len(cap['nodes']), 1)
        self.assertEqual(cap['history']['cpu']['workspace'], [[1000, 1.0], [1060, 1.5]])

    def test_prom_error_is_captured_not_raised(self):
        def boom(expr):
            raise controller.PromError('prometheus unreachable')
        controller.prom_instant_multi = boom

        cap = controller.cluster_capacity()
        self.assertEqual(cap['metricsError'], 'prometheus unreachable')
        self.assertIsNone(cap['cluster'])
        self.assertEqual(cap['nodes'], [])


if __name__ == '__main__':
    unittest.main()
