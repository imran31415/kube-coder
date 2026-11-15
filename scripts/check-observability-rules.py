#!/usr/bin/env python3
"""Validate the actual Helm-rendered rules using helm and promtool on PATH."""
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def render(*sets):
    rendered = subprocess.check_output([
        'helm', 'template', 'observability-test', str(ROOT / 'charts/workspace-controller'),
        '-f', str(ROOT / 'charts/workspace-controller/tests/test-values.yaml'),
        '--set', 'controller.metrics.prometheusRule.enabled=true',
        *[arg for value in sets for arg in ('--set', value)],
        '--api-versions', 'monitoring.coreos.com/v1/PrometheusRule',
        '--show-only', 'templates/prometheus-rules.yaml'], text=True)
    # The isolated template has one top-level spec. Strip its indentation to
    # feed the Prometheus rule format directly; no YAML package is required.
    spec = rendered.split('\nspec:\n', 1)[1]
    return '\n'.join(line[2:] if line.startswith('  ') else line for line in spec.splitlines()) + '\n'


def main():
    # The scenarios cover every rule, so test the full set; the default set
    # (no missing-scrape alert) must still be valid on its own.
    rules = render('controller.metrics.prometheusRule.workspaceScrapeAlert=true')
    default_rules = render()
    if 'WorkspaceMetricsUnavailable' in default_rules:
        raise SystemExit('WorkspaceMetricsUnavailable must be opt-in (workspaceScrapeAlert)')
    tests = ROOT / 'charts/workspace-controller/tests'
    with tempfile.TemporaryDirectory(prefix='kc-alerts-') as directory:
        folder = pathlib.Path(directory)
        (folder / 'rules.yaml').write_text(rules, encoding='utf-8')
        (folder / 'default-rules.yaml').write_text(default_rules, encoding='utf-8')
        for name in ('alerts.promtool.yaml', 'alerts-default.promtool.yaml'):
            (folder / name).write_text((tests / name).read_text(encoding='utf-8'), encoding='utf-8')
        subprocess.run(['promtool', 'check', 'rules', 'rules.yaml', 'default-rules.yaml'], cwd=folder, check=True)
        subprocess.run(['promtool', 'test', 'rules', 'alerts.promtool.yaml',
                        'alerts-default.promtool.yaml'], cwd=folder, check=True)


if __name__ == '__main__':
    main()
