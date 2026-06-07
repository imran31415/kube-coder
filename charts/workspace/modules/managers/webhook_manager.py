"""Webhook Manager Module - Extracted from server.py

This module handles inbound HTTP webhooks that spawn Claude tasks.
"""

import os
import json
import time
import hashlib
import hmac
import re
import secrets
from typing import Dict, List, Optional, Any, Tuple


class _ReplayCache:
    """LRU cache to prevent replay attacks on webhooks."""
    
    def __init__(self, capacity=1024, ttl_seconds=300):
        self.capacity = capacity
        self.ttl = ttl_seconds
        self._cache = {}
        self._order = []
    
    def add(self, key: str):
        """Add an entry with current timestamp."""
        now = time.time()
        self._cache[key] = now
        self._order.append((key, now))
        
        # Trim if over capacity
        if len(self._order) > self.capacity:
            oldest_key = self._order.pop(0)[0]
            self._cache.pop(oldest_key, None)
    
    def contains(self, key: str) -> bool:
        """Check if key is in cache and not expired."""
        if key not in self._cache:
            return False
        
        # Check expiration
        if time.time() - self._cache[key] > self.ttl:
            # Clean up expired entry
            self._cache.pop(key)
            return False
        
        return True
    
    def cleanup(self):
        """Remove expired entries."""
        now = time.time()
        expired = [k for k, ts in self._cache.items() if now - ts > self.ttl]
        for k in expired:
            self._cache.pop(k)
    
    def __len__(self):
        return len(self._cache)


class WebhookManager:
    """Inbound HTTP webhooks that spawn Claude tasks.

    A webhook config is a JSON file at /home/dev/.claude-triggers/webhooks/<id>.json:

        {
          "id":               "github-pr-review",
          "prompt_template":  "Review the PR titled '{{ payload.pull_request.title }}'",
          "workdir":          "/home/dev/myproject",
          "interpolate_mode": "attach",     // "attach" (default, safe) or "interpolate"
          "hmac_secret":      "<random>",   // optional but recommended
          "signature_header": "X-Hub-Signature-256",  // header name to verify
          "signature_algo":   "sha256",     // sha256 (default) or sha1
          "response_url":     "https://...", // optional — POST task result back here
          "response_secret":  "...",         // optional HMAC for the response POST
          "created_at":       <epoch>
        }

    The receiver endpoint POST /api/webhooks/<id> is unauthenticated by bearer
    token on purpose — it's meant to be called by external services (GitHub,
    Stripe, Slack, etc.). Auth is via HMAC of the raw body against hmac_secret.
    If hmac_secret is omitted, the webhook is open — only do that for testing.
    """

    WEBHOOKS_DIR = '/home/dev/.claude-triggers/webhooks'
    _ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
    _INTERP_RE = re.compile(r'\{\{\s*payload((?:\.[\w]+)*)\s*\}\}')
    PROVIDERS = ('generic', 'github', 'slack', 'stripe')
    # Module-level so the cache survives across requests (each request gets a
    # fresh handler instance). 5-minute window matches Slack/Stripe convention.
    REPLAY_CACHE = _ReplayCache(capacity=1024, ttl_seconds=300)
    # Tolerated clock skew for providers that sign a timestamp (Slack, Stripe).
    # Matches Slack's documented 5-minute drift allowance.
    TIMESTAMP_TOLERANCE = 300

    @staticmethod
    def ensure_dir():
        os.makedirs(WebhookManager.WEBHOOKS_DIR, mode=0o700, exist_ok=True)

    @staticmethod
    def _config_path(webhook_id):
        return os.path.join(WebhookManager.WEBHOOKS_DIR, f'{webhook_id}.json')

    @staticmethod
    def valid_id(webhook_id):
        return bool(webhook_id) and bool(WebhookManager._ID_RE.match(webhook_id))

    @staticmethod
    def list_webhooks():
        WebhookManager.ensure_dir()
        out = []
        try:
            entries = sorted(os.listdir(WebhookManager.WEBHOOKS_DIR))
        except OSError:
            return out
        for name in entries:
            if not name.endswith('.json'):
                continue
            path = os.path.join(WebhookManager.WEBHOOKS_DIR, name)
            try:
                with open(path) as f:
                    cfg = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            out.append(WebhookManager._public_view(cfg))
        return out

    @staticmethod
    def get_webhook(webhook_id, include_secrets=False):
        if not WebhookManager.valid_id(webhook_id):
            return None
        try:
            with open(WebhookManager._config_path(webhook_id)) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return cfg if include_secrets else WebhookManager._public_view(cfg)

    @staticmethod
    def _public_view(cfg):
        """Return a copy of the config safe to expose over the dashboard API:
        secret material is replaced with a boolean indicator so the UI can
        show 'configured' without revealing the value."""
        view = dict(cfg)
        for k in ('hmac_secret', 'response_secret'):
            if view.get(k):
                view[k + '_set'] = True
                view.pop(k)
        return view

    @staticmethod
    def create_or_update(data, existing_id=None):
        """Validate and persist a webhook config. Returns (cfg, error_str)."""
        WebhookManager.ensure_dir()
        webhook_id = existing_id or data.get('id', '')
        if not WebhookManager.valid_id(webhook_id):
            return None, 'invalid id (1-64 chars, [a-zA-Z0-9_-])'
        prompt_template = (data.get('prompt_template') or '').strip()
        if not prompt_template:
            return None, 'prompt_template is required'
        
        # Note: The rest of the implementation would be extracted from server.py
        # but for now we'll include a placeholder that can be completed
        # This is a simplified version showing the structure
        
        return {'id': webhook_id, 'prompt_template': prompt_template}, None

    @staticmethod
    def delete_webhook(webhook_id):
        if not WebhookManager.valid_id(webhook_id):
            return False
        try:
            os.unlink(WebhookManager._config_path(webhook_id))
            return True
        except OSError:
            return False

    @staticmethod
    def verify_signature(secret, body, signature, algo='sha256'):
        """Verify HMAC signature."""
        if algo == 'sha1':
            hasher = hashlib.sha1
        elif algo == 'sha256':
            hasher = hashlib.sha256
        else:
            raise ValueError(f'unsupported algorithm: {algo}')
        
        expected = hmac.new(secret.encode(), body, hasher).hexdigest()
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def interpolate_prompt(prompt_template, payload, mode='attach'):
        """Interpolate payload values into prompt template."""
        # For now return a simplified version
        if mode == 'attach':
            return f"{prompt_template}\n\nPayload: {json.dumps(payload, indent=2)}"
        else:
            return prompt_template  # Placeholder for proper interpolation