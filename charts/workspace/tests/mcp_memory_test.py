"""Unit tests for mcp_memory.py — the MCP server wrapping MemoryManager.

Covers identity/provenance, TTL helper, every tool handler (happy path +
not-found/validation errors), the content/JSON-RPC framing, and the
dispatch/serve loop (notifications, unknown method, bad params, batch,
invalid JSON).

DB-touching tests use the isolated-SQLite-store pattern from memory_test.py.
_send is patched to capture JSON-RPC frames without touching stdout.

Run with:    python3 -m unittest tests.mcp_memory_test
(from charts/workspace/)
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import memory.store as _store_mod  # noqa: E402
import mcp_memory as mcp  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


# ───────────────────────────────────────────────────────────────────────────
# Pure helpers (no DB)
# ───────────────────────────────────────────────────────────────────────────

class ActorTests(unittest.TestCase):
    def test_default_actor(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KC_TASK_ID', None)
            self.assertEqual(mcp._actor(), 'mcp:unknown')

    def test_task_actor(self):
        with mock.patch.dict(os.environ, {'KC_TASK_ID': 'abc'}, clear=False):
            self.assertEqual(mcp._actor(), 'task:abc')


class ExpiresAtTests(unittest.TestCase):
    def test_none_passthrough(self):
        self.assertIsNone(mcp._expires_at_from_days(None))

    def test_valid_days_future(self):
        out = mcp._expires_at_from_days(1)
        self.assertIsNotNone(out)
        self.assertGreater(out, time.time())

    def test_invalid_days(self):
        # A non-numeric string raises ValueError inside the helper → None.
        self.assertIsNone(mcp._expires_at_from_days('not-a-number'))


class ContentTextTests(unittest.TestCase):
    def test_wraps_json_text(self):
        out = mcp._content_text({'a': 1})
        self.assertEqual(out[0]['type'], 'text')
        self.assertEqual(json.loads(out[0]['text']), {'a': 1})

    def test_non_serializable_falls_back_to_str(self):
        out = mcp._content_text({'t': object()})
        self.assertIn('text', out[0])  # default=str prevents a raise


# ───────────────────────────────────────────────────────────────────────────
# Tool handlers (DB-touching)
# ───────────────────────────────────────────────────────────────────────────

class McpDBTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._db = os.path.join(self._tmpdir.name, 'memory.db')
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(self._db)
        self.addCleanup(self._restore)

    def _restore(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init

    def remember(self, ns='user.t', key='k', value='v', **extra):
        return mcp._tool_remember({'namespace': ns, 'key': key, 'value': value, **extra})


class ToolHandlerTests(McpDBTestCase):
    def test_remember_then_recall(self):
        row = self.remember(value='hello')
        self.assertEqual(row['namespace'], 'user.t')
        self.assertEqual(row['value'], 'hello')
        recalled = mcp._tool_recall({'namespace': 'user.t', 'key': 'k'})
        self.assertEqual(recalled['value'], 'hello')

    def test_recall_missing_raises_notfound(self):
        from memory.manager import NotFound
        with self.assertRaises(NotFound):
            mcp._tool_recall({'namespace': 'no', 'key': 'where'})

    def test_remember_with_importance_and_ttl(self):
        row = self.remember(importance=0.9, expires_in_days=2, kind='preference')
        self.assertEqual(row['kind'], 'preference')

    def test_update_changes_value(self):
        self.remember(value='old')
        row = mcp._tool_update({'namespace': 'user.t', 'key': 'k', 'value': 'new'})
        self.assertEqual(row['value'], 'new')
        self.assertGreaterEqual(row['version'], 2)

    def test_search_returns_envelope(self):
        self.remember(key='a', value='alpha apple')
        out = mcp._tool_search({'q': 'alpha'})
        self.assertIn('results', out)
        self.assertEqual(out['query'], 'alpha')
        self.assertEqual(out['count'], len(out['results']))

    def test_list_returns_envelope(self):
        self.remember(key='a')
        self.remember(key='b')
        out = mcp._tool_list({'namespace': 'user.t'})
        self.assertEqual(out['count'], 2)
        self.assertEqual(len(out['memories']), 2)

    def test_link_and_neighbors(self):
        self.remember(key='a')
        self.remember(key='b')
        mcp._tool_link({'src_namespace': 'user.t', 'src_key': 'a',
                        'dst_namespace': 'user.t', 'dst_key': 'b'})
        out = mcp._tool_neighbors({'namespace': 'user.t', 'key': 'a'})
        self.assertGreaterEqual(out['count'], 1)
        keys = {n['key'] for n in out['neighbors']}
        self.assertIn('b', keys)

    def test_forget_soft_deletes(self):
        self.remember(key='gone')
        mcp._tool_forget({'namespace': 'user.t', 'key': 'gone'})
        self.assertIsNone(MemoryManager.get(namespace='user.t', key='gone'))

    def test_stats_returns_dict(self):
        self.remember()
        out = mcp._tool_stats({})
        self.assertIsInstance(out, dict)

    def test_tool_call_dispatch_and_unknown(self):
        self.remember()
        self.assertIn('memories', mcp._tool_call('memory_list', {'namespace': 'user.t'}))
        with self.assertRaises(ValueError):
            mcp._tool_call('memory_bogus', {})


# ───────────────────────────────────────────────────────────────────────────
# MCP method dispatch / framing
# ───────────────────────────────────────────────────────────────────────────

class HandleToolsCallTests(McpDBTestCase):
    def test_success_is_not_error(self):
        self.remember()
        res = mcp._handle_tools_call({'name': 'memory_list', 'arguments': {'namespace': 'user.t'}})
        self.assertFalse(res['isError'])
        body = json.loads(res['content'][0]['text'])
        self.assertEqual(body['count'], 1)

    def test_notfound_is_error_with_code(self):
        res = mcp._handle_tools_call({'name': 'memory_recall',
                                      'arguments': {'namespace': 'x', 'key': 'y'}})
        self.assertTrue(res['isError'])
        body = json.loads(res['content'][0]['text'])
        self.assertIn('error', body)

    def test_non_string_name_raises_valueerror(self):
        with self.assertRaises(ValueError):
            mcp._handle_tools_call({'name': 123})

    def test_unknown_tool_is_error(self):
        res = mcp._handle_tools_call({'name': 'memory_nope', 'arguments': {}})
        self.assertTrue(res['isError'])


class StaticHandlerTests(unittest.TestCase):
    def test_initialize(self):
        out = mcp._handle_initialize({})
        self.assertEqual(out['protocolVersion'], mcp.PROTOCOL_VERSION)
        self.assertEqual(out['serverInfo']['name'], 'kube-coder-memory')

    def test_tools_list(self):
        out = mcp._handle_tools_list({})
        names = {t['name'] for t in out['tools']}
        self.assertIn('memory_remember', names)
        self.assertIn('memory_forget', names)


class DispatchTests(unittest.TestCase):
    def _capture(self, req):
        with mock.patch.object(mcp, '_send') as send:
            mcp._dispatch(req)
        return send

    def test_non_dict_request_errors(self):
        send = self._capture(['not', 'a', 'dict'])
        self.assertEqual(send.call_args[0][0]['error']['code'], mcp.INVALID_REQUEST)

    def test_notification_no_id_is_swallowed(self):
        send = self._capture({'method': 'notifications/initialized'})
        send.assert_not_called()

    def test_unknown_method(self):
        send = self._capture({'id': 1, 'method': 'bogus'})
        self.assertEqual(send.call_args[0][0]['error']['code'], mcp.METHOD_NOT_FOUND)

    def test_value_error_maps_to_invalid_params(self):
        with mock.patch.dict(mcp._METHODS, {'boom': mock.Mock(side_effect=ValueError('bad'))}):
            send = self._capture({'id': 2, 'method': 'boom'})
        self.assertEqual(send.call_args[0][0]['error']['code'], mcp.INVALID_PARAMS)

    def test_internal_error_on_generic_exception(self):
        with mock.patch.dict(mcp._METHODS, {'boom': mock.Mock(side_effect=RuntimeError('x'))}):
            send = self._capture({'id': 3, 'method': 'boom'})
        self.assertEqual(send.call_args[0][0]['error']['code'], mcp.INTERNAL_ERROR)

    def test_success_replies_with_result(self):
        send = self._capture({'id': 4, 'method': 'initialize'})
        self.assertIn('result', send.call_args[0][0])


class ServeTests(unittest.TestCase):
    def _serve(self, text):
        import io
        with mock.patch.object(sys, 'stdin', io.StringIO(text)), \
             mock.patch.object(mcp, '_dispatch') as disp, \
             mock.patch.object(mcp, '_error') as err:
            mcp._serve()
        return disp, err

    def test_skips_blank_lines(self):
        disp, err = self._serve('\n   \n')
        disp.assert_not_called()
        err.assert_not_called()

    def test_invalid_json_errors(self):
        disp, err = self._serve('{bad json\n')
        err.assert_called_once()
        self.assertEqual(err.call_args[0][1], mcp.INVALID_REQUEST)

    def test_batch_dispatches_each_element(self):
        line = json.dumps([{'id': 1, 'method': 'initialize'},
                           {'id': 2, 'method': 'initialize'}])
        disp, _ = self._serve(line + '\n')
        self.assertEqual(disp.call_count, 2)

    def test_single_request_dispatched(self):
        disp, _ = self._serve(json.dumps({'id': 1, 'method': 'initialize'}) + '\n')
        disp.assert_called_once()


class MainTests(unittest.TestCase):
    def test_main_returns_zero_on_clean_serve(self):
        with mock.patch.object(mcp, '_serve'):
            self.assertEqual(mcp.main(), 0)

    def test_main_returns_one_on_fatal(self):
        with mock.patch.object(mcp, '_serve', side_effect=RuntimeError('boom')):
            self.assertEqual(mcp.main(), 1)

    def test_main_swallows_keyboard_interrupt(self):
        with mock.patch.object(mcp, '_serve', side_effect=KeyboardInterrupt):
            self.assertEqual(mcp.main(), 0)


if __name__ == '__main__':
    unittest.main()
