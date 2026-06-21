"""Unit tests for memory/embeddings.py — the Phase-2 embedding providers (#90).

Covers provider selection (get_provider), the disabled/no-key paths, and each
provider's embed() over a mocked client/HTTP — no network, no API key needed.

Run with:    python3 -m unittest tests.embeddings_test
(from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import memory.embeddings as emb  # noqa: E402


class GetProviderTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertIsNone(emb.get_provider({}))
        for v in ('none', 'off', 'disabled', '', '0', 'false', 'NONE'):
            self.assertIsNone(emb.get_provider({'KC_EMBED_PROVIDER': v}), v)

    def test_voyage_requires_key(self):
        self.assertIsNone(emb.get_provider({'KC_EMBED_PROVIDER': 'voyage'}))
        p = emb.get_provider({'KC_EMBED_PROVIDER': 'voyage', 'KC_VOYAGE_API_KEY': 'k'})
        self.assertIsInstance(p, emb.VoyageProvider)
        self.assertEqual(p.dim, emb.DEFAULT_DIM)
        self.assertEqual(p.model, emb.VoyageProvider.DEFAULT_MODEL)

    def test_voyage_accepts_legacy_env_key(self):
        p = emb.get_provider({'KC_EMBED_PROVIDER': 'voyage', 'VOYAGE_API_KEY': 'k'})
        self.assertIsInstance(p, emb.VoyageProvider)

    def test_openai_requires_key(self):
        self.assertIsNone(emb.get_provider({'KC_EMBED_PROVIDER': 'openai'}))
        p = emb.get_provider({'KC_EMBED_PROVIDER': 'openai', 'KC_OPENAI_API_KEY': 'k'})
        self.assertIsInstance(p, emb.OpenAIProvider)

    def test_model_and_dim_overrides(self):
        p = emb.get_provider({
            'KC_EMBED_PROVIDER': 'voyage', 'KC_VOYAGE_API_KEY': 'k',
            'KC_EMBED_MODEL': 'voyage-3-lite', 'KC_EMBED_DIM': '512',
        })
        self.assertEqual(p.model, 'voyage-3-lite')
        self.assertEqual(p.dim, 512)

    def test_bad_dim_falls_back_to_default(self):
        p = emb.get_provider({
            'KC_EMBED_PROVIDER': 'voyage', 'KC_VOYAGE_API_KEY': 'k',
            'KC_EMBED_DIM': 'not-a-number',
        })
        self.assertEqual(p.dim, emb.DEFAULT_DIM)

    def test_unknown_provider_raises(self):
        with self.assertRaises(emb.EmbeddingError):
            emb.get_provider({'KC_EMBED_PROVIDER': 'cohere'})

    def test_uses_os_environ_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KC_EMBED_PROVIDER', None)
            self.assertIsNone(emb.get_provider())


class VoyageEmbedTests(unittest.TestCase):
    def setUp(self):
        self.provider = emb.VoyageProvider('key', 'voyage-3', 1024)

    def _fake_voyageai(self, embeddings=None, raise_exc=None):
        client = mock.Mock()
        if raise_exc is not None:
            client.embed.side_effect = raise_exc
        else:
            client.embed.return_value = mock.Mock(embeddings=embeddings or [])
        module = types.SimpleNamespace(Client=mock.Mock(return_value=client))
        return module, client

    def test_empty_input_short_circuits(self):
        self.assertEqual(self.provider.embed([]), [])

    def test_returns_embeddings(self):
        module, client = self._fake_voyageai(embeddings=[[0.1, 0.2], [0.3, 0.4]])
        with mock.patch.dict(sys.modules, {'voyageai': module}):
            out = self.provider.embed(['a', 'b'])
        self.assertEqual(out, [[0.1, 0.2], [0.3, 0.4]])
        client.embed.assert_called_once()
        self.assertEqual(client.embed.call_args.kwargs['model'], 'voyage-3')

    def test_wraps_errors(self):
        module, _ = self._fake_voyageai(raise_exc=RuntimeError('boom'))
        with mock.patch.dict(sys.modules, {'voyageai': module}):
            with self.assertRaises(emb.EmbeddingError):
                self.provider.embed(['a'])


class OpenAIEmbedTests(unittest.TestCase):
    def setUp(self):
        self.provider = emb.OpenAIProvider('key', 'text-embedding-3-small', 1024)

    def test_empty_input_short_circuits(self):
        self.assertEqual(self.provider.embed([]), [])

    def test_returns_embeddings_sorted_by_index(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        # Deliberately out of order — must be re-sorted by `index`.
        resp.json.return_value = {'data': [
            {'index': 1, 'embedding': [0.3, 0.4]},
            {'index': 0, 'embedding': [0.1, 0.2]},
        ]}
        import httpx
        with mock.patch.object(httpx, 'post', return_value=resp) as post:
            out = self.provider.embed(['a', 'b'])
        self.assertEqual(out, [[0.1, 0.2], [0.3, 0.4]])
        body = post.call_args.kwargs['json']
        self.assertEqual(body['dimensions'], 1024)
        self.assertEqual(body['model'], 'text-embedding-3-small')

    def test_custom_base_url(self):
        p = emb.OpenAIProvider('k', base_url='http://proxy/v1/')
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {'data': [{'index': 0, 'embedding': [1.0]}]}
        import httpx
        with mock.patch.object(httpx, 'post', return_value=resp) as post:
            p.embed(['x'])
        self.assertTrue(post.call_args.args[0].startswith('http://proxy/v1/embeddings'))

    def test_wraps_http_errors(self):
        import httpx
        with mock.patch.object(httpx, 'post', side_effect=RuntimeError('net')):
            with self.assertRaises(emb.EmbeddingError):
                self.provider.embed(['a'])


if __name__ == '__main__':
    unittest.main()
