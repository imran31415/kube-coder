"""Unit tests for page_watch.py — the deterministic half of a page-watch (#681).

These cover extraction, selector scoping, normalization, hash versioning and
the creation-time redirect walk. They touch no network and no filesystem: the
module is pure by construction, which is the point of splitting it out of
server.py.

Run: python3 -m unittest tests.page_watch_test   (from charts/workspace/)
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import page_watch  # noqa: E402


# ── selector parsing ─────────────────────────────────────────────────────

class SelectorParsingTests(unittest.TestCase):
    def test_accepts_the_documented_subset(self):
        for sel in ('div', '#main', '.status', 'div.card', 'span#id.a.b',
                    '[data-state="green"]', '[hidden]', '.build .status',
                    'div.card span'):
            self.assertIsNotNone(page_watch.parse_selector(sel), msg=sel)

    def test_rejects_unsupported_combinators_by_name(self):
        for sel, word in (('.a > .b', 'child'), ('.a + .b', 'adjacent'),
                          ('.a ~ .b', 'general-sibling'),
                          ('.a, .b', 'selector lists'),
                          ('a:hover', 'pseudo-classes')):
            with self.assertRaises(page_watch.SelectorSyntaxError) as cm:
                page_watch.parse_selector(sel)
            # The message must name the subset, not just say "invalid" — the
            # limit is surprising and the user cannot guess it.
            self.assertIn('Supported:', str(cm.exception), msg=sel)

    def test_blank_selector_means_whole_page(self):
        self.assertIsNone(page_watch.parse_selector(None))
        self.assertIsNone(page_watch.parse_selector('   '))

    def test_absurdly_long_selector_refused(self):
        with self.assertRaises(page_watch.SelectorSyntaxError):
            page_watch.parse_selector('.x' * 200)

    def test_attribute_selector_with_colon_in_name_is_allowed(self):
        # ':' is legal inside [attr] (xml:lang) but is a pseudo-class outside.
        self.assertIsNotNone(page_watch.parse_selector('[xml:lang="en"]'))
        with self.assertRaises(page_watch.SelectorSyntaxError):
            page_watch.parse_selector('div:first-child')


# ── extraction ───────────────────────────────────────────────────────────

class ExtractionTests(unittest.TestCase):
    def x(self, html, selector=None, **kw):
        return page_watch.normalize(
            page_watch.extract_text(html.encode('utf-8'), selector, **kw))

    def test_plain_text_of_a_document(self):
        self.assertEqual(
            self.x('<html><body><h1>Build</h1><p>passing</p></body></html>'),
            'Build passing')

    def test_script_style_and_template_are_suppressed(self):
        html = ('<body>keep'
                '<script>var nonce="abc123";</script>'
                '<style>.a{color:red}</style>'
                '<template><p>inert</p></template>'
                'end</body>')
        self.assertEqual(self.x(html), 'keep end')

    def test_comments_are_dropped_without_splitting_text(self):
        # A comment is invisible and creates no word boundary, exactly as a
        # browser renders it. This is load-bearing for noise suppression:
        # if a comment split the text, then merely ADDING a comment would
        # change the hash and fire the watch.
        self.assertEqual(self.x('<p>a<!-- csrf=9f3 -->b</p>'), 'ab')
        self.assertEqual(self.x('<p>ab</p>'), self.x('<p>a<!--x-->b</p>'))

    def test_attributes_never_contribute_text(self):
        # This is the mechanism that kills most page noise for free.
        a = self.x('<div id="r-1" data-nonce="aaa" class="x">status</div>')
        b = self.x('<div id="r-2" data-nonce="zzz" class="x">status</div>')
        self.assertEqual(a, b)
        self.assertEqual(a, 'status')

    def test_entities_are_unescaped(self):
        self.assertEqual(self.x('<p>a &amp; b &lt;c&gt;</p>'), 'a & b <c>')

    def test_empty_body_is_a_valid_empty_extraction(self):
        # An empty page is a legitimate state, not an error.
        self.assertEqual(
            page_watch.extract_text(b''), '')

    def test_void_elements_do_not_corrupt_the_stack(self):
        html = '<div><br><img src="a"><hr>text<input></div><p>after</p>'
        self.assertEqual(self.x(html), 'text after')

    def test_malformed_unclosed_tags_still_terminate(self):
        self.assertEqual(self.x('<div><span>a<div>b'), 'a b')

    def test_stray_end_tag_is_ignored(self):
        self.assertEqual(self.x('</div><p>a</p></span>'), 'a')


class SelectorScopingTests(unittest.TestCase):
    """AC #3 — a change outside the selected subtree must not be visible."""

    PAGE = ('<body><header>clock {t}</header>'
            '<div class="build"><span class="status">{s}</span></div>'
            '<footer>ads {t}</footer></body>')

    def x(self, html, selector):
        return page_watch.normalize(
            page_watch.extract_text(html.encode('utf-8'), selector))

    def test_selector_isolates_its_subtree(self):
        a = self.x(self.PAGE.format(t='10:00', s='failing'), '.status')
        b = self.x(self.PAGE.format(t='23:59', s='failing'), '.status')
        self.assertEqual(a, b, 'noise outside the selector leaked in')
        self.assertEqual(a, 'failing')

    def test_change_inside_the_subtree_is_visible(self):
        a = self.x(self.PAGE.format(t='10:00', s='failing'), '.status')
        b = self.x(self.PAGE.format(t='10:00', s='passing'), '.status')
        self.assertNotEqual(a, b)

    def test_descendant_chain(self):
        self.assertEqual(
            self.x(self.PAGE.format(t='1', s='green'), '.build .status'),
            'green')

    def test_descendant_chain_requires_the_ancestor(self):
        html = '<div class="other"><span class="status">x</span></div>'
        with self.assertRaises(page_watch.SelectorNoMatch):
            self.x(html, '.build .status')

    def test_multiple_matches_concatenate_in_document_order(self):
        html = '<ul><li class="j">one</li><li class="j">two</li></ul>'
        self.assertEqual(self.x(html, '.j'), 'one two')

    def test_no_match_is_an_error_not_an_empty_page(self):
        # Critical: reporting this as '' would hash to a new value and fire a
        # "change" that is really a broken watch.
        with self.assertRaises(page_watch.SelectorNoMatch):
            self.x('<p>nothing here</p>', '.status')

    def test_matched_but_empty_subtree_is_not_an_error(self):
        self.assertEqual(self.x('<div class="status"></div>', '.status'), '')

    def test_attribute_selector_scoping(self):
        html = '<i data-state="green">ok</i><i data-state="red">bad</i>'
        self.assertEqual(self.x(html, '[data-state="green"]'), 'ok')


class SvgBadgeTests(unittest.TestCase):
    """A CI status badge is SVG — the primary use case, not an edge case."""

    BADGE = ('<svg xmlns="http://www.w3.org/2000/svg" width="106">'
             '<g><text x="10" y="14">build</text>'
             '<text x="70" y="14">{s}</text></g></svg>')

    def test_badge_text_is_extracted(self):
        out = page_watch.normalize(page_watch.extract_text(
            self.BADGE.format(s='passing').encode('utf-8'),
            content_type='image/svg+xml'))
        self.assertIn('passing', out)

    def test_badge_flip_changes_the_hash(self):
        red, _ = page_watch.fingerprint(
            self.BADGE.format(s='failing').encode('utf-8'))
        green, _ = page_watch.fingerprint(
            self.BADGE.format(s='passing').encode('utf-8'))
        self.assertNotEqual(red, green)

    def test_badge_selector_narrows_to_the_status_text(self):
        out = page_watch.normalize(page_watch.extract_text(
            self.BADGE.format(s='passing').encode('utf-8'), 'text'))
        self.assertEqual(out, 'build passing')


class DecodingTests(unittest.TestCase):
    def test_meta_charset_is_honoured(self):
        body = ('<meta charset="iso-8859-1"><p>caf\xe9</p>'
                .encode('iso-8859-1'))
        self.assertEqual(
            page_watch.normalize(page_watch.extract_text(body)), 'caf\xe9')

    def test_content_type_header_wins_over_meta(self):
        body = '<meta charset="ascii"><p>caf\xe9</p>'.encode('utf-8')
        out = page_watch.extract_text(
            body, content_type='text/html; charset=utf-8')
        self.assertIn('caf\xe9', out)

    def test_invalid_bytes_never_raise_and_stay_stable(self):
        body = b'<p>ok \xff\xfe bad</p>'
        a = page_watch.normalize(page_watch.extract_text(body))
        b = page_watch.normalize(page_watch.extract_text(body))
        self.assertEqual(a, b, 'undecodable bytes must hash stably')

    def test_utf8_bom_is_stripped(self):
        self.assertEqual(
            page_watch.normalize(
                page_watch.extract_text('﻿<p>hi</p>'.encode('utf-8'))),
            'hi')

    def test_unknown_charset_falls_back_without_raising(self):
        body = b'<meta charset="definitely-not-a-codec"><p>hi</p>'
        self.assertEqual(
            page_watch.normalize(page_watch.extract_text(body)), 'hi')


class TruncationTests(unittest.TestCase):
    def test_truncated_read_is_an_error_not_a_change(self):
        # The cut-off point drifts with unrelated content above it, so hashing
        # a truncated body would fire a "change" on nearly every check.
        with self.assertRaises(page_watch.ExtractError):
            page_watch.extract_text(b'<p>partial', truncated=True)


# ── normalization + hashing ──────────────────────────────────────────────

class NormalizeTests(unittest.TestCase):
    def test_all_whitespace_runs_collapse(self):
        self.assertEqual(
            page_watch.normalize('  a\n\n\tb \r\n  c  '), 'a b c')

    def test_reflow_alone_does_not_change_the_text(self):
        self.assertEqual(page_watch.normalize('a\nb'),
                         page_watch.normalize('a     b'))

    def test_none_and_empty(self):
        self.assertEqual(page_watch.normalize(None), '')
        self.assertEqual(page_watch.normalize(''), '')


class HashTests(unittest.TestCase):
    def test_hash_is_stable_and_prefixed(self):
        h = page_watch.content_hash('passing')
        self.assertEqual(h, page_watch.content_hash('passing'))
        self.assertTrue(h.startswith('sha256:'))

    def test_different_text_different_hash(self):
        self.assertNotEqual(page_watch.content_hash('passing'),
                            page_watch.content_hash('failing'))

    def test_normalizer_version_is_mixed_in(self):
        # Bumping the version must change every hash, so a rules change
        # re-baselines rather than being mistaken for unchanged content.
        original = page_watch.NORMALIZER_VERSION
        before = page_watch.content_hash('x')
        try:
            page_watch.NORMALIZER_VERSION = original + 1
            after = page_watch.content_hash('x')
        finally:
            page_watch.NORMALIZER_VERSION = original
        self.assertNotEqual(before, after)

    def test_fingerprint_returns_hash_and_text(self):
        h, text = page_watch.fingerprint(b'<p> hello  world </p>')
        self.assertEqual(text, 'hello world')
        self.assertEqual(h, page_watch.content_hash('hello world'))


# ── prompt-payload safety ────────────────────────────────────────────────

class ScanExcerptTests(unittest.TestCase):
    def test_metadata_only_by_default(self):
        out = page_watch.scan_excerpt('secret page text')
        self.assertFalse(out['content_included'])
        self.assertNotIn('excerpt', out)

    def test_opt_in_includes_the_excerpt(self):
        out = page_watch.scan_excerpt('build passing', include_content=True)
        self.assertTrue(out['content_included'])
        self.assertEqual(out['excerpt'], 'build passing')

    def test_high_severity_finding_suppresses_rather_than_strips(self):
        scanner = lambda t: [{'severity': 'high', 'what': 'tag block'}]
        out = page_watch.scan_excerpt('looks fine', include_content=True,
                                      scanner=scanner)
        self.assertTrue(out['content_suppressed'])
        self.assertFalse(out['content_included'])
        self.assertNotIn('excerpt', out)
        self.assertEqual(out['hidden_findings'], 1)

    def test_low_severity_passes_through_with_a_count(self):
        scanner = lambda t: [{'severity': 'low'}]
        out = page_watch.scan_excerpt('ok', include_content=True,
                                      scanner=scanner)
        self.assertTrue(out['content_included'])
        self.assertEqual(out['hidden_findings'], 1)

    def test_scanner_failure_fails_closed(self):
        def boom(_):
            raise RuntimeError('scanner exploded')
        out = page_watch.scan_excerpt('ok', include_content=True,
                                      scanner=boom)
        self.assertTrue(out['content_suppressed'])
        self.assertFalse(out['content_included'])

    def test_excerpt_is_capped(self):
        out = page_watch.scan_excerpt('x' * 5000, include_content=True)
        self.assertEqual(len(out['excerpt']), page_watch.MAX_EXCERPT_CHARS)
        self.assertTrue(out['excerpt_truncated'])

    def test_real_instruction_scan_catches_hidden_tag_block(self):
        # Wire the actual detector, not a stub, so the integration is proven.
        try:
            import instruction_scan
        except ImportError:
            self.skipTest('instruction_scan unavailable')
        hidden = 'build passing' + ''.join(chr(0xE0000 + ord(c))
                                           for c in 'rm -rf /')
        out = page_watch.scan_excerpt(hidden, include_content=True,
                                      scanner=instruction_scan.scan_text)
        self.assertTrue(out['content_suppressed'],
                        'invisible tag-block instructions must suppress the '
                        'excerpt before it reaches the agent')


# ── redirect resolution ──────────────────────────────────────────────────

class RedirectTests(unittest.TestCase):
    def fetch_script(self, *steps):
        """Build a fetch stub that replays (status, headers, body) in order."""
        calls = []
        seq = list(steps)

        def fake(url, **kw):
            calls.append(url)
            if not seq:
                raise AssertionError(f'no stubbed response for {url}')
            return seq.pop(0)
        return fake, calls

    def test_no_redirect_returns_the_original(self):
        fetch, calls = self.fetch_script((200, {}, b'ok'))
        url, status, _, body = page_watch.resolve_redirects(
            'https://e.test/a', fetch=fetch)
        self.assertEqual((url, status, body), ('https://e.test/a', 200, b'ok'))
        self.assertEqual(calls, ['https://e.test/a'])

    def test_follows_and_returns_the_final_url(self):
        fetch, calls = self.fetch_script(
            (301, {'Location': 'https://e.test/b'}, b''),
            (200, {}, b'done'))
        url, status, _, _ = page_watch.resolve_redirects(
            'https://e.test/a', fetch=fetch)
        self.assertEqual(url, 'https://e.test/b')
        self.assertEqual(status, 200)
        # Each hop is a fresh fetch — which is what re-runs the SSRF guard.
        self.assertEqual(calls, ['https://e.test/a', 'https://e.test/b'])

    def test_relative_location_is_resolved(self):
        fetch, _ = self.fetch_script(
            (302, {'Location': '/builds/latest'}, b''),
            (200, {}, b'ok'))
        url, _, _, _ = page_watch.resolve_redirects(
            'https://e.test/a/b', fetch=fetch)
        self.assertEqual(url, 'https://e.test/builds/latest')

    def test_header_name_case_is_ignored(self):
        fetch, _ = self.fetch_script(
            (308, {'location': 'https://e.test/z'}, b''),
            (200, {}, b'ok'))
        url, _, _, _ = page_watch.resolve_redirects(
            'https://e.test/a', fetch=fetch)
        self.assertEqual(url, 'https://e.test/z')

    def test_https_to_http_downgrade_refused(self):
        fetch, _ = self.fetch_script(
            (302, {'Location': 'http://e.test/plain'}, b''))
        with self.assertRaises(page_watch.PageWatchError) as cm:
            page_watch.resolve_redirects('https://e.test/a', fetch=fetch)
        self.assertIn('downgrade', str(cm.exception))

    def test_redirect_loop_refused(self):
        fetch, _ = self.fetch_script(
            (302, {'Location': 'https://e.test/b'}, b''),
            (302, {'Location': 'https://e.test/a'}, b''))
        with self.assertRaises(page_watch.PageWatchError) as cm:
            page_watch.resolve_redirects('https://e.test/a', fetch=fetch)
        self.assertIn('loop', str(cm.exception))

    def test_too_many_hops_refused(self):
        fetch, _ = self.fetch_script(
            *[(302, {'Location': f'https://e.test/{i}'}, b'')
              for i in range(6)])
        with self.assertRaises(page_watch.PageWatchError) as cm:
            page_watch.resolve_redirects('https://e.test/start', fetch=fetch,
                                         max_hops=3)
        self.assertIn('redirects', str(cm.exception))

    def test_redirect_without_location_is_an_error(self):
        fetch, _ = self.fetch_script((301, {}, b''))
        with self.assertRaises(page_watch.PageWatchError) as cm:
            page_watch.resolve_redirects('https://e.test/a', fetch=fetch)
        self.assertIn('Location', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
