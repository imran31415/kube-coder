"""Security hardening utilities for kube-coder.

This module provides security enhancements including:
- Rate limiting for API endpoints
- Security headers middleware
- Input validation utilities
- Security context enhancements
"""

import time
import hashlib
import ipaddress
from typing import Dict, List, Optional, Tuple, Any
from threading import Lock
from collections import defaultdict


class RateLimiter:
    """Simple rate limiter for API endpoints."""
    
    def __init__(self, requests_per_minute: int = 60, cleanup_interval: int = 300):
        """
        Args:
            requests_per_minute: Maximum requests per minute per key
            cleanup_interval: How often to clean up old entries (seconds)
        """
        self.requests_per_minute = requests_per_minute
        self.cleanup_interval = cleanup_interval
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = Lock()
        self._last_cleanup = time.time()
    
    def is_allowed(self, key: str) -> Tuple[bool, Optional[float]]:
        """Check if request is allowed.
        
        Returns:
            Tuple[bool, Optional[float]]: (allowed, wait_time_seconds)
        """
        with self._lock:
            self._cleanup_old_entries()
            
            now = time.time()
            window_start = now - 60  # 1 minute window
            
            # Filter requests within time window
            recent_requests = [t for t in self._requests[key] if t > window_start]
            self._requests[key] = recent_requests
            
            if len(recent_requests) >= self.requests_per_minute:
                # Calculate wait time
                oldest_request = min(recent_requests)
                wait_time = 60 - (now - oldest_request)
                return False, max(0, wait_time)
            
            # Add current request
            self._requests[key].append(now)
            return True, None
    
    def _cleanup_old_entries(self):
        """Clean up entries older than cleanup_interval."""
        now = time.time()
        if now - self._last_cleanup < self.cleanup_interval:
            return
        
        cutoff = now - 120  # Keep last 2 minutes of data
        to_delete = []
        
        for key, timestamps in self._requests.items():
            recent = [t for t in timestamps if t > cutoff]
            if recent:
                self._requests[key] = recent
            else:
                to_delete.append(key)
        
        for key in to_delete:
            del self._requests[key]
        
        self._last_cleanup = now
    
    def get_stats(self, key: str) -> Dict[str, Any]:
        """Get rate limiting statistics for a key."""
        with self._lock:
            now = time.time()
            window_start = now - 60
            recent_requests = [t for t in self._requests.get(key, []) if t > window_start]
            
            return {
                'requests_in_last_minute': len(recent_requests),
                'limit': self.requests_per_minute,
                'oldest_request': min(recent_requests) if recent_requests else None,
                'newest_request': max(recent_requests) if recent_requests else None
            }


class SecurityHeaders:
    """Security headers middleware."""
    
    DEFAULT_HEADERS = {
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'X-XSS-Protection': '1; mode=block',
        'Referrer-Policy': 'strict-origin-when-cross-origin',
        'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    }
    
    # Strict CSP for production
    STRICT_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    
    @classmethod
    def add_headers(cls, handler, csp_strict: bool = False):
        """Add security headers to HTTP response.
        
        Args:
            handler: HTTP handler instance
            csp_strict: Whether to use strict CSP (for production)
        """
        # Add default headers
        for name, value in cls.DEFAULT_HEADERS.items():
            handler.send_header(name, value)
        
        # Add CSP header based on strictness
        if csp_strict:
            handler.send_header('Content-Security-Policy', cls.STRICT_CSP)
        else:
            # More permissive CSP for development
            dev_csp = cls.STRICT_CSP.replace("'self'", "'self' 'unsafe-inline' 'unsafe-eval'")
            handler.send_header('Content-Security-Policy', dev_csp)
        
        # Add HSTS for HTTPS (in production)
        # handler.send_header('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')


class InputValidator:
    """Input validation utilities."""
    
    @staticmethod
    def validate_command(command: str, allowed_patterns: List[str] = None) -> bool:
        """Validate shell command for safe execution.
        
        Args:
            command: Command string to validate
            allowed_patterns: List of regex patterns that are allowed
            
        Returns:
            bool: True if command appears safe
        """
        import re
        
        if not command or not isinstance(command, str):
            return False
        
        # Default dangerous patterns to block
        dangerous_patterns = [
            r'`.*`',                     # Backticks
            r'\$\(.*\)',                 # Command substitution
            r'\|\s*.*\s*;\s*.*',         # Pipeline with semicolon
            r'&\s*.*',                   # Background processes
            r'\|\s*.*\|\s*.*',           # Multiple pipes
            r'>\s*/dev/',                # Device redirection
            r'rm\s+-[rf]\s+',            # Forceful rm
            r':(){.*};:',                # Fork bomb pattern
            r'mkfs',                     # Filesystem destruction
            r'dd\s+.*if=/dev/',          # Disk manipulation
        ]
        
        # Check against dangerous patterns
        for pattern in dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return False
        
        # Check against allowed patterns if provided
        if allowed_patterns:
            for pattern in allowed_patterns:
                if re.match(pattern, command):
                    return True
            return False
        
        return True
    
    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Sanitize filename to prevent path traversal.
        
        Args:
            filename: Original filename
            
        Returns:
            str: Sanitized filename
        """
        import os
        
        # Remove directory components
        basename = os.path.basename(filename)
        
        # Remove null bytes and control characters
        basename = ''.join(c for c in basename if ord(c) >= 32)
        
        # Limit length
        if len(basename) > 255:
            basename = basename[:255]
        
        return basename
    
    @staticmethod
    def validate_ip_address(ip: str) -> bool:
        """Validate IP address."""
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False
    
    @staticmethod
    def validate_url(url: str, allowed_domains: List[str] = None) -> bool:
        """Validate URL and optionally check against allowed domains.
        
        Args:
            url: URL to validate
            allowed_domains: List of allowed domains (None allows all)
            
        Returns:
            bool: True if URL appears valid
        """
        import urllib.parse
        
        try:
            result = urllib.parse.urlparse(url)
            
            # Basic URL validation
            if not all([result.scheme, result.netloc]):
                return False
            
            # Check scheme
            if result.scheme not in ['http', 'https', '']:
                return False
            
            # Check against allowed domains if specified
            if allowed_domains:
                domain = result.netloc.split(':')[0]  # Remove port
                if domain not in allowed_domains:
                    return False
            
            return True
        except Exception:
            return False


class SecurityContext:
    """Container security context utilities."""
    
    @staticmethod
    def get_secure_context(privileged: bool = False) -> Dict[str, Any]:
        """Get secure Kubernetes security context.
        
        Args:
            privileged: Whether container needs privileged access
            
        Returns:
            Dict with security context configuration
        """
        base_context = {
            'capabilities': {
                'drop': ['ALL']
            },
            'readOnlyRootFilesystem': False,
            'runAsNonRoot': True,
            'runAsUser': 1000,
            'runAsGroup': 1000,
            'seccompProfile': {
                'type': 'RuntimeDefault'
            }
        }
        
        if privileged:
            # For containers that need privilege (e.g., dind)
            return {
                'privileged': True,
                'capabilities': {
                    'add': ['ALL']
                }
            }
        
        return base_context
    
    @staticmethod
    def get_pod_security_context() -> Dict[str, Any]:
        """Get secure Pod security context."""
        return {
            'fsGroup': 1000,
            'runAsNonRoot': True,
            'runAsUser': 1000,
            'runAsGroup': 1000,
            'seccompProfile': {
                'type': 'RuntimeDefault'
            }
        }


# Global rate limiter instances
api_limiter = RateLimiter(requests_per_minute=300)  # 5 requests per second
auth_limiter = RateLimiter(requests_per_minute=30)   # 30 requests per minute for auth
webhook_limiter = RateLimiter(requests_per_minute=600)  # 10 requests per second for webhooks