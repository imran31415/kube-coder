"""Package canonical stdlib telemetry helpers into the standalone controller chart."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ('prometheus_exposition.py', 'observability_metrics.py',
         'observability_http.py', 'observability_logging.py')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    stale = []
    for name in FILES:
        source = ROOT / 'charts/workspace' / name
        target = ROOT / 'charts/workspace-controller' / name
        data = source.read_bytes()
        if args.check:
            if not target.exists() or target.read_bytes() != data:
                stale.append(name)
        else:
            target.write_bytes(data)
    if stale:
        parser.exit(1, 'Run python scripts/sync-observability.py: ' + ', '.join(stale) + '\n')


if __name__ == '__main__':
    main()
