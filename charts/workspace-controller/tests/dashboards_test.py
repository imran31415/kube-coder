"""Guards for the Grafana dashboards in charts/workspace-controller/dashboards/.

The dashboards are hand-maintained JSON (edit in Grafana, export, replace the
file), so nothing else notices when a metric they query is renamed or removed:
the panel just goes blank on someone's cluster. These tests fail first.
"""
import json
import re
import unittest
from pathlib import Path

CHART = Path(__file__).resolve().parents[1]
REPO_CHARTS = CHART.parent
DASHBOARDS = sorted((CHART / 'dashboards').glob('*.json'))

# Where kubecoder_* metrics are defined. Workspace product metrics are built
# as `cls.PREFIX + '<name>'` in server.py; HTTP and controller-operation metrics
# are literal names in the observability helpers.
SOURCES = [
    REPO_CHARTS / 'workspace' / 'server.py',
    REPO_CHARTS / 'workspace' / 'observability_metrics.py',
    CHART / 'observability_metrics.py',
]
HISTOGRAM_SUFFIXES = ('_bucket', '_count', '_sum')
METRIC_RE = re.compile(r'\bkubecoder_[a-z0-9_]+')


def defined_metrics():
    names = set()
    for src in SOURCES:
        text = src.read_text()
        names.update(METRIC_RE.findall(text))
        names.update('kubecoder_' + m for m in re.findall(r"PREFIX \+ '([a-z0-9_]+)'", text))
    return names


def targets(dashboard):
    for panel in dashboard['panels']:
        for target in panel.get('targets', []):
            yield panel, target


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DASHBOARDS, 'no dashboards found')
        self.dashboards = {p.name: json.loads(p.read_text()) for p in DASHBOARDS}

    def test_uids_are_unique_and_stable(self):
        uids = [d['uid'] for d in self.dashboards.values()]
        self.assertEqual(len(uids), len(set(uids)))
        for name, d in self.dashboards.items():
            # The ConfigMap is named after the file; keep uid == file stem so
            # links and bookmarks survive a rename of the title.
            self.assertEqual(d['uid'], Path(name).stem, name)
            self.assertIn('kube-coder', d['tags'], name)

    def test_every_query_uses_the_datasource_variable(self):
        # A hard-coded datasource uid only exists on the cluster it was
        # exported from; everyone else gets "datasource not found".
        for name, d in self.dashboards.items():
            var_names = [v['name'] for v in d['templating']['list']]
            self.assertIn('datasource', var_names, name)
            for panel, target in targets(d):
                for obj in (panel, target):
                    self.assertEqual(obj['datasource']['uid'], '${datasource}',
                                     f"{name}: {panel['title']}")

    def test_every_kubecoder_metric_queried_exists(self):
        known = defined_metrics()
        self.assertIn('kubecoder_tasks', known)  # the source scan itself works
        for name, d in self.dashboards.items():
            for panel, target in targets(d):
                for metric in METRIC_RE.findall(target['expr']):
                    base = metric
                    for suffix in HISTOGRAM_SUFFIXES:
                        if metric.endswith(suffix) and metric[:-len(suffix)] in known:
                            base = metric[:-len(suffix)]
                    self.assertIn(base, known,
                                  f"{name}: panel {panel['title']!r} queries {metric}, "
                                  "which no kube-coder source defines")

    def test_panel_ids_unique_and_queries_present(self):
        for name, d in self.dashboards.items():
            ids = [p['id'] for p in d['panels']]
            self.assertEqual(len(ids), len(set(ids)), name)
            for panel in d['panels']:
                if panel['type'] != 'row':
                    self.assertTrue(panel.get('targets'), f"{name}: {panel['title']}")
                    for t in panel['targets']:
                        self.assertTrue(t['expr'].strip(), f"{name}: {panel['title']}")

    def test_no_deployment_specific_values(self):
        # The repo is shared: no workspace namespace, cluster host or exported
        # Grafana instance identifiers baked into a dashboard.
        for name in self.dashboards:
            text = (CHART / 'dashboards' / name).read_text()
            self.assertNotRegex(text, r'"ws-[a-z0-9]', name)
            self.assertNotIn('__inputs', text, f'{name}: exported with "share externally"; '
                             'export without it so the datasource variable is kept')
            self.assertNotRegex(text, r'https?://', name)


if __name__ == '__main__':
    unittest.main()
