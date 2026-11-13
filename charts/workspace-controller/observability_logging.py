"""JSON stderr logging and safe, request-correlated errors (stdlib only)."""
import contextvars
import datetime
import json
import logging
import re
import sys
import traceback
import uuid

CONTEXT = contextvars.ContextVar('observability_context', default={})
_SECRET = re.compile(r'(?i)(bearer\s+|(?:token|password|secret|api[_-]?key)[\s=:]+)[^\s,;]+')
_URL = re.compile(r'https?://[^\s]+')


def safe_text(value):
    """Defense in depth for legacy messages; new events use allowlisted fields."""
    return _URL.sub('[url]', _SECRET.sub(r'\1[redacted]', str(value)))[:2000]


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'level': record.levelname, 'service': record.name,
                'event': getattr(record, 'event', 'application'),
                'message': safe_text(record.getMessage()), **CONTEXT.get()}
        data.update(getattr(record, 'fields', {}))
        if record.exc_info:
            # Stack locations aid debugging without serializing arbitrary exception
            # values (which can contain credentials, upstream bodies or URLs).
            data['exception_type'] = record.exc_info[0].__name__
            data['traceback'] = [{'file': f.filename, 'line': f.lineno, 'function': f.name}
                                 for f in traceback.extract_tb(record.exc_info[2])]
        return json.dumps(data, ensure_ascii=True, default=str)


class StderrHandler(logging.StreamHandler):
    def emit(self, record):
        # Respect process-level stderr capture in tests and service runners.
        self.stream = sys.stderr
        super().emit(record)


def logger(service):
    log = logging.getLogger(service)
    if not log.handlers:
        handler = StderrHandler()
        handler.setFormatter(JsonFormatter())
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def event(service, name, level=logging.INFO, exc_info=None, **fields):
    logger(service).log(level, name, extra={'event': name, 'fields': fields}, exc_info=exc_info)


def legacy_log(service, *values, sep=' ', end='\n', file=None, flush=False):
    """Migration adapter for existing diagnostic prints; not a stdout redirect."""
    message = sep.join(str(v) for v in values)
    level = logging.ERROR if any(w in message.lower() for w in ('failed', 'error', 'exception')) else logging.INFO
    logger(service).log(level, message, exc_info=sys.exc_info() if sys.exc_info()[0] else None)


def problem(service, code='internal_error', error_id=None, exc_info=None):
    messages = {
        'internal_error': "The request could not be completed. Try again.",
        'metrics_disabled': 'Monitoring is not configured.',
        'metrics_timeout': 'Monitoring took too long to respond.',
        'metrics_unavailable': 'Monitoring is temporarily unavailable.',
        'metrics_not_reporting': 'No workspace metrics received yet.',
    }
    result = {'error': messages[code], 'code': code,
              'retryable': code not in ('metrics_disabled', 'metrics_not_reporting'),
              'error_id': error_id or uuid.uuid4().hex[:12],
              'request_id': CONTEXT.get().get('request_id')}
    event(service, code, logging.ERROR if code == 'internal_error' else logging.WARNING,
          exc_info=exc_info, error_id=result['error_id'])
    return result
