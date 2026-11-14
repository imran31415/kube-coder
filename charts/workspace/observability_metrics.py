"""Bounded process-local telemetry. No I/O while holding a registry lock."""
from collections import defaultdict
from contextlib import contextmanager
import math
import threading
import time

from prometheus_exposition import Exposition

BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1., 2.5, 5., 10., 30., math.inf)


class ProcessCounters:
    """Existing hook-counter contract; monotonic for the process lifetime."""
    _lock = threading.Lock()
    _values = {}

    @classmethod
    def inc(cls, name, labels=None, amount=1):
        if amount < 0:
            raise ValueError('counter increments must be nonnegative')
        key = (name, tuple(sorted((labels or {}).items())))
        with cls._lock:
            cls._values[key] = cls._values.get(key, 0) + amount

    @classmethod
    def get(cls, name):
        with cls._lock:
            return {k[1]: v for k, v in cls._values.items() if k[0] == name}

    @classmethod
    def value(cls, name, labels=None):
        return cls.get(name).get(tuple(sorted((labels or {}).items())), 0)


def status_class(status):
    """`2xx`..`5xx`: five values instead of one series per distinct code."""
    try:
        status = int(status)
    except (TypeError, ValueError):
        return 'other'
    return f'{status // 100}xx' if 100 <= status <= 599 else 'other'


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = defaultdict(int)
        self._active = defaultdict(int)
        self._aborted = defaultdict(int)
        self._durations = {}
        self._operations = defaultdict(int)
        self._operation_durations = {}

    @staticmethod
    def _observe(table, key, elapsed):
        if not math.isfinite(elapsed) or elapsed < 0:
            return
        counts, total = table.get(key, ([0] * len(BUCKETS), 0.))
        for i, boundary in enumerate(BUCKETS):
            counts[i] += int(elapsed <= boundary)
        table[key] = (counts, total + elapsed)

    def active(self, service, listener, kind, change):
        with self._lock:
            self._active[(service, listener, kind)] += change

    def request(self, service, listener, route, method, status, kind, elapsed, aborted=False):
        with self._lock:
            if status is not None:
                self._counts[(service, listener, route, method, status_class(status))] += 1
            if aborted:
                self._aborted[(service, listener, kind)] += 1
            if kind == 'request' and status is not None:
                self._observe(self._durations, (service, listener, route, method), elapsed)

    @contextmanager
    def operation(self, name):
        started = time.monotonic()
        result = {'outcome': 'success'}
        try:
            yield result
        except Exception:
            result['outcome'] = 'error'
            raise
        finally:
            with self._lock:
                self._operations[(name, result['outcome'])] += 1
                self._observe(self._operation_durations, (name,), time.monotonic() - started)

    def exposition(self):
        with self._lock:
            counts, active, aborted, operations = (dict(x) for x in (
                self._counts, self._active, self._aborted, self._operations))
            durations, op_durations = ({k: (list(v[0]), v[1]) for k, v in x.items()}
                                      for x in (self._durations, self._operation_durations))
        out = Exposition()
        def samples(values, names):
            return [(dict(zip(names, k)), v) for k, v in sorted(values.items())]
        labels = ('service', 'listener', 'route', 'method')
        out.counter('kubecoder_http_requests_total', 'HTTP final responses by status class (2xx..5xx).', samples(counts, (*labels, 'status')))
        out.gauge('kubecoder_http_requests_in_flight', 'Active requests and streams.', samples(active, ('service', 'listener', 'kind')))
        out.counter('kubecoder_http_aborted_requests_total', 'Disconnected requests.', samples(aborted, ('service', 'listener', 'kind')))
        out.counter('kubecoder_controller_operations_total', 'Controller action outcomes; submitted is not completed.', samples(operations, ('operation', 'outcome')))
        for name, values, names in (('kubecoder_http_request_duration_seconds', durations, labels),
                                     ('kubecoder_controller_operation_duration_seconds', op_durations, ('operation',))):
            out.histogram(name, 'Completed operation duration in seconds.',
                          [(dict(zip(names, key)), list(zip(BUCKETS, counts)), total)
                           for key, (counts, total) in sorted(values.items())])
        return out.render()


REGISTRY = Registry()
