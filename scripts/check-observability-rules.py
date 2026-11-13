#!/usr/bin/env python3
"""Validate the actual Helm-rendered rules using helm and promtool on PATH."""
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main():
    rendered = subprocess.check_output([
        'helm', 'template', 'observability-test', str(ROOT / 'charts/workspace-controller'),
        '-f', str(ROOT / 'charts/workspace-controller/tests/test-values.yaml'),
        '--set', 'controller.metrics.prometheusRule.enabled=true',
        '--api-versions', 'monitoring.coreos.com/v1/PrometheusRule',
        '--show-only', 'templates/prometheus-rules.yaml'], text=True)
    # The isolated template has one top-level spec. Strip its indentation to
    # feed the Prometheus rule format directly; no YAML package is required.
    spec = rendered.split('\nspec:\n', 1)[1]
    rules = '\n'.join(line[2:] if line.startswith('  ') else line for line in spec.splitlines()) + '\n'
    fixture = (ROOT / 'charts/workspace-controller/tests/alerts.promtool.yaml').read_text(encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='kc-alerts-') as directory:
        folder = pathlib.Path(directory)
        (folder / 'rules.yaml').write_text(rules, encoding='utf-8')
        (folder / 'test.yaml').write_text(fixture, encoding='utf-8')
        subprocess.run(['promtool', 'check', 'rules', 'rules.yaml'], cwd=folder, check=True)
        subprocess.run(['promtool', 'test', 'rules', 'test.yaml'], cwd=folder, check=True)


if __name__ == '__main__':
    main()
