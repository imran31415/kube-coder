"""Unit tests for server.py's DocsManager and AppsManager.

DocsManager: path-safe markdown serving from a docs dir + manifest.
AppsManager: /proc/net/tcp[6] listen-port discovery, hex address decoders,
loopback classification, pin CRUD, and the merged Applications view.

Both are filesystem/parsing logic with injectable paths — no HTTP handler
needed. DOCS_DIR / PINS_PATH are redirected to tempdirs; /proc fixtures
are passed explicitly.

Run with:    python3 -m unittest tests.server_docs_apps_test
(from charts/workspace/)
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

DM = server.DocsManager
AM = server.AppsManager


# ───────────────────────── DocsManager ─────────────────────────

class DocsManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = DM.DOCS_DIR
        DM.DOCS_DIR = self.tmp
        DM._PAGE_CACHE = {}
        DM._MANIFEST_CACHE = (0.0, None)
        self.addCleanup(self._restore)

    def _restore(self):
        DM.DOCS_DIR = self._orig_dir
        DM._PAGE_CACHE = {}
        DM._MANIFEST_CACHE = (0.0, None)

    def _write(self, rel, content):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)

    def _manifest(self):
        self._write('_manifest.json', json.dumps({
            'version': 1,
            'sections': [{
                'id': 'intro', 'title': 'Intro',
                'pages': [{'id': 'start', 'title': 'Getting Started',
                           'file': 'start.md', 'summary': 'how to start'}],
            }],
        }))
        self._write('start.md', '# Getting Started\n\nInstall kube-coder and run make local.\n')

    # _safe_join
    def test_safe_join_normal(self):
        self.assertEqual(DM._safe_join('a.md'), os.path.realpath(os.path.join(self.tmp, 'a.md')))

    def test_safe_join_rejects_traversal(self):
        for bad in ('../etc/passwd', '..', '\x00x'):
            with self.assertRaises(ValueError):
                DM._safe_join(bad)

    def test_safe_join_rejects_escape(self):
        with self.assertRaises(ValueError):
            DM._safe_join('sub/../../outside')

    # manifest / index / pages
    def test_load_manifest_missing_returns_default(self):
        self.assertEqual(DM.load_manifest(), {'version': 1, 'sections': []})

    def test_load_manifest_and_cache(self):
        self._manifest()
        m1 = DM.load_manifest()
        self.assertEqual(m1['version'], 1)
        # Second call hits cache (same mtime) — still correct.
        self.assertIs(DM.load_manifest(), m1)

    def test_index_flattens_pages(self):
        self._manifest()
        idx = DM.index()
        self.assertIn('start', idx['pages'])
        self.assertEqual(idx['pages']['start']['section_id'], 'intro')

    def test_get_page_returns_markdown(self):
        self._manifest()
        page = DM.get_page('start')
        self.assertEqual(page['title'], 'Getting Started')
        self.assertIn('make local', page['markdown'])

    def test_get_page_unknown_id_raises(self):
        self._manifest()
        with self.assertRaises(KeyError):
            DM.get_page('nope')

    def test_get_page_missing_file_raises(self):
        self._write('_manifest.json', json.dumps({
            'sections': [{'id': 's', 'pages': [{'id': 'p', 'file': 'gone.md'}]}]}))
        with self.assertRaises(KeyError):
            DM.get_page('p')

    # search
    def test_search_empty_query(self):
        self.assertEqual(DM.search(''), [])

    def test_search_title_outranks_body(self):
        self._manifest()
        self._write('other.md', 'install install install\n')
        # Add a second page that only matches in body.
        self._write('_manifest.json', json.dumps({
            'sections': [{'id': 'intro', 'title': 'Intro', 'pages': [
                {'id': 'start', 'title': 'Getting Started', 'file': 'start.md'},
                {'id': 'inst', 'title': 'Install Guide', 'file': 'other.md'},
            ]}]}))
        DM._MANIFEST_CACHE = (0.0, None)
        res = DM.search('install')
        self.assertTrue(res)
        # 'Install Guide' has the term in its title → highest score.
        self.assertEqual(res[0]['id'], 'inst')

    def test_search_no_match(self):
        self._manifest()
        self.assertEqual(DM.search('zzzznotpresent'), [])


# ───────────────────────── AppsManager ─────────────────────────

class HexDecodeTests(unittest.TestCase):
    def test_ipv4_loopback(self):
        # /proc stores bytes little-endian: 0100007F -> 127.0.0.1
        self.assertEqual(AM._decode_ipv4_hex('0100007F'), '127.0.0.1')

    def test_ipv4_non_loopback(self):
        self.assertEqual(AM._decode_ipv4_hex('0100000A'), '10.0.0.1')

    def test_ipv4_bad_length(self):
        self.assertIsNone(AM._decode_ipv4_hex('FF'))

    def test_ipv6_loopback_and_unspecified(self):
        self.assertEqual(AM._decode_ipv6_hex('0' * 32), '::')
        self.assertEqual(AM._decode_ipv6_hex('00000000000000000000000001000000'), '::1')

    def test_ipv6_bad_length(self):
        self.assertIsNone(AM._decode_ipv6_hex('abcd'))


class LoopbackTests(unittest.TestCase):
    def test_loopback_addrs(self):
        for a in ('127.0.0.1', '::1', '0.0.0.0', '::', '::ffff:127.0.0.1'):
            self.assertTrue(AM._is_loopback(a), a)

    def test_non_loopback(self):
        for a in ('10.0.0.1', '192.168.1.5', '::ffff:8.8.8.8'):
            self.assertFalse(AM._is_loopback(a), a)


class ParseListenPortsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _tcp(self, *rows):
        path = os.path.join(self.tmp, 'tcp')
        header = ('  sl  local_address rem_address   st tx_queue rx_queue tr '
                  'tm->when retrnsmt   uid  timeout inode\n')
        with open(path, 'w') as f:
            f.write(header + '\n'.join(rows) + '\n')
        return path

    def _row(self, local_hex, state='0A', inode='12345'):
        # 17-ish fields; only parts[1]=local, [3]=state, [9]=inode matter.
        return (f'   0: {local_hex} 00000000:0000 {state} 00000000:00000000 '
                f'00:00000000 00000000  1000  0 {inode} 1 0000 100 0 0 10 0')

    def test_listen_loopback_port_returned(self):
        tcp = self._tcp(self._row('0100007F:1538'))  # 0x1538 = 5432
        out = AM.parse_listen_ports(tcp_path=tcp, tcp6_path='/nonexistent')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['port'], 5432)
        self.assertEqual(out[0]['addr'], '127.0.0.1')

    def test_non_listen_state_skipped(self):
        tcp = self._tcp(self._row('0100007F:1538', state='01'))  # ESTABLISHED
        self.assertEqual(AM.parse_listen_ports(tcp_path=tcp, tcp6_path='/nonexistent'), [])

    def test_non_loopback_skipped(self):
        tcp = self._tcp(self._row('0100000A:1538'))  # 10.0.0.1
        self.assertEqual(AM.parse_listen_ports(tcp_path=tcp, tcp6_path='/nonexistent'), [])

    def test_missing_files_yield_empty(self):
        self.assertEqual(AM.parse_listen_ports('/nope', '/nope6'), [])


class ValidatorTests(unittest.TestCase):
    def test_validate_port_ok(self):
        self.assertEqual(AM._validate_port('8000'), 8000)

    def test_validate_port_range_and_type(self):
        for bad in (0, 70000, 'abc', None):
            with self.assertRaises(ValueError):
                AM._validate_port(bad)

    def test_validate_name_ok(self):
        self.assertEqual(AM._validate_name('  My App-1 '), 'My App-1')

    def test_validate_name_empty_and_bad_chars(self):
        with self.assertRaises(ValueError):
            AM._validate_name('')
        with self.assertRaises(ValueError):
            AM._validate_name('bad\nname')


class PinCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = AM.PINS_PATH
        AM.PINS_PATH = os.path.join(self.tmp, 'apps.json')
        self.addCleanup(lambda: setattr(AM, 'PINS_PATH', self._orig))

    def test_add_get_remove_round_trip(self):
        AM.add_pin(8000, 'Django', strip_prefix=True)
        pin = AM.get_pin(8000)
        self.assertEqual(pin['name'], 'Django')
        self.assertTrue(pin['strip_prefix'])
        self.assertTrue(AM.remove_pin(8000))
        self.assertIsNone(AM.get_pin(8000))

    def test_remove_nonexistent_is_false(self):
        self.assertFalse(AM.remove_pin(9999))

    def test_get_pin_invalid_port_none(self):
        self.assertIsNone(AM.get_pin('not-a-port'))

    def test_load_pins_missing_corrupt_nondict(self):
        self.assertEqual(AM._load_pins(), {})  # missing
        with open(AM.PINS_PATH, 'w') as f:
            f.write('{bad json')
        self.assertEqual(AM._load_pins(), {})  # corrupt
        with open(AM.PINS_PATH, 'w') as f:
            f.write('[1,2,3]')
        self.assertEqual(AM._load_pins(), {})  # non-dict

    def test_load_pins_coerces_int_keys(self):
        with open(AM.PINS_PATH, 'w') as f:
            json.dump({'8000': {'name': 'x'}, 'bad': {'name': 'y'}}, f)
        pins = AM._load_pins()
        self.assertIn(8000, pins)
        self.assertNotIn('bad', pins)


class MergedViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = AM.PINS_PATH
        AM.PINS_PATH = os.path.join(self.tmp, 'apps.json')
        self.addCleanup(lambda: setattr(AM, 'PINS_PATH', self._orig))

    def test_list_apps_merges_pins_and_listeners(self):
        AM.add_pin(8000, 'My App')  # pinned but not listening -> stopped
        with mock.patch.object(
                AM, 'parse_listen_ports',
                return_value=[{'port': 9000, 'addr': '127.0.0.1', 'inode': 1}]):
            rows = AM.list_apps()
        by_port = {r['port']: r for r in rows}
        self.assertEqual(by_port[8000]['status'], 'stopped')
        self.assertTrue(by_port[8000]['pinned'])
        self.assertEqual(by_port[9000]['status'], 'running')
        self.assertFalse(by_port[9000]['pinned'])

    def test_list_apps_marks_internal_port_blocked(self):
        AM.add_pin(8080, 'reserved')  # 8080 is in INTERNAL_PORTS
        with mock.patch.object(AM, 'parse_listen_ports', return_value=[]):
            rows = AM.list_apps()
        self.assertEqual(rows[0]['status'], 'blocked')

    def test_is_proxyable(self):
        with mock.patch.object(
                AM, 'parse_listen_ports',
                return_value=[{'port': 9000, 'addr': '127.0.0.1', 'inode': 1}]):
            self.assertEqual(AM.is_proxyable(9000), (True, ''))
            ok, reason = AM.is_proxyable(9001)
            self.assertFalse(ok)
        # internal + invalid
        self.assertFalse(AM.is_proxyable(8080)[0])
        self.assertFalse(AM.is_proxyable('xyz')[0])


if __name__ == '__main__':
    import unittest.mock  # noqa: F401  (used above via unittest.mock)
    unittest.main()
