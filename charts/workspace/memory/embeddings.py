"""Embedding providers for semantic (vector) memory search — Phase 2 (issue #90).

A thin, dependency-light abstraction over an embedding API so the background
worker can turn memory text into vectors and hybrid search can embed queries.
Providers are selected by environment, and the whole feature **no-ops when no
provider/key is configured**, so Phase-1 deployments are unaffected.

Configuration (all optional; absence = feature disabled):

  KC_EMBED_PROVIDER   voyage | openai | none        (default: none)
  KC_EMBED_MODEL      provider model id             (sensible default per provider)
  KC_EMBED_DIM        output dimension              (default 1024)
  KC_VOYAGE_API_KEY   Voyage credential   (also accepts VOYAGE_API_KEY)
  KC_OPENAI_API_KEY   OpenAI credential   (also accepts OPENAI_API_KEY)
  KC_OPENAI_BASE_URL  OpenAI-compatible base URL    (default api.openai.com/v1)

The `vec_memories` virtual table is declared `FLOAT[1024]` (memory/store.py),
so the default dimension is 1024 — pick models that emit (or can be reduced
to) that width: Voyage `voyage-3` (1024) or OpenAI `text-embedding-3-small`
with the `dimensions` parameter.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

# Must match the width of the vec_memories FLOAT[N] table in memory/store.py.
DEFAULT_DIM = 1024


class EmbeddingError(Exception):
    """Raised when an embedding call fails (network, auth, bad response)."""


def _to_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class EmbeddingProvider:
    """Interface: turn a batch of texts into equal-length float vectors."""

    name = 'base'

    def __init__(self, model: str, dim: int):
        self.model = model
        self.dim = dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f'<{type(self).__name__} model={self.model!r} dim={self.dim}>'


class VoyageProvider(EmbeddingProvider):
    name = 'voyage'
    DEFAULT_MODEL = 'voyage-3'

    def __init__(self, api_key: str, model: Optional[str] = None, dim: int = DEFAULT_DIM):
        super().__init__(model or self.DEFAULT_MODEL, dim)
        self._api_key = api_key
        self._client = None

    def _client_lazy(self):
        # Imported lazily so the module loads even when voyageai isn't present.
        if self._client is None:
            import voyageai
            self._client = voyageai.Client(api_key=self._api_key)
        return self._client

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            result = self._client_lazy().embed(
                texts, model=self.model, input_type='document')
            return list(result.embeddings)
        except Exception as e:  # provider lib raises its own error types
            raise EmbeddingError(f'voyage embed failed: {e}') from e


class OpenAIProvider(EmbeddingProvider):
    name = 'openai'
    DEFAULT_MODEL = 'text-embedding-3-small'

    def __init__(self, api_key: str, model: Optional[str] = None,
                 dim: int = DEFAULT_DIM, base_url: Optional[str] = None):
        super().__init__(model or self.DEFAULT_MODEL, dim)
        self._api_key = api_key
        self._base = (base_url or 'https://api.openai.com/v1').rstrip('/')

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        import httpx
        body: Dict[str, object] = {
            'model': self.model,
            'input': texts,
            'dimensions': self.dim,
        }
        try:
            resp = httpx.post(
                f'{self._base}/embeddings',
                headers={'Authorization': f'Bearer {self._api_key}'},
                json=body,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()['data']
            # The API doesn't guarantee order; sort by the echoed index.
            return [d['embedding'] for d in sorted(data, key=lambda d: d.get('index', 0))]
        except EmbeddingError:
            raise
        except Exception as e:
            raise EmbeddingError(f'openai embed failed: {e}') from e


def get_provider(env: Optional[Dict[str, str]] = None) -> Optional[EmbeddingProvider]:
    """Build the configured embedding provider, or None when the feature is
    disabled / unconfigured (no provider selected, or selected but no key).

    Raises EmbeddingError only for an explicitly-unknown provider name.
    """
    env = env if env is not None else os.environ  # type: ignore[assignment]
    name = (env.get('KC_EMBED_PROVIDER') or '').strip().lower()
    if name in ('', 'none', 'off', 'disabled', '0', 'false'):
        return None

    dim = _to_int(env.get('KC_EMBED_DIM'), DEFAULT_DIM)
    model = (env.get('KC_EMBED_MODEL') or '').strip() or None

    if name == 'voyage':
        key = env.get('KC_VOYAGE_API_KEY') or env.get('VOYAGE_API_KEY')
        return VoyageProvider(key, model, dim) if key else None
    if name == 'openai':
        key = env.get('KC_OPENAI_API_KEY') or env.get('OPENAI_API_KEY')
        if not key:
            return None
        return OpenAIProvider(key, model, dim, base_url=env.get('KC_OPENAI_BASE_URL'))

    raise EmbeddingError(f'unknown KC_EMBED_PROVIDER: {name!r}')
