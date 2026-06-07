#!/usr/bin/env python3
"""
Standardized HTTP response utilities for kube-coder.

Provides consistent HTTP response patterns, error handling, and routing
utilities across server.py and controller.py.
"""

import json
import time
import traceback
from typing import Any, Dict, Optional, Union, Callable, Tuple
from http import HTTPStatus
import http.server

# -------------------------------------------------------------------
# HTTP Response Constants
# -------------------------------------------------------------------

DEFAULT_CHARSET = 'utf-8'
JSON_CONTENT_TYPE = f'application/json; charset={DEFAULT_CHARSET}'
TEXT_CONTENT_TYPE = f'text/plain; charset={DEFAULT_CHARSET}'
HTML_CONTENT_TYPE = f'text/html; charset={DEFAULT_CHARSET}'
EVENT_STREAM_CONTENT_TYPE = 'text/event-stream'

# Standard cache control headers
NO_CACHE_HEADERS = {
    'Cache-Control': 'no-cache, no-store, must-revalidate',
    'Pragma': 'no-cache'
}

IMMUTABLE_CACHE_HEADERS = {
    'Cache-Control': 'public, max-age=31536000, immutable'
}

# -------------------------------------------------------------------
# Error Types
# -------------------------------------------------------------------

class HTTPError(Exception):
    """Base exception for HTTP errors with status code and message."""
    def __init__(self, status_code: int, message: str = '', 
                 error_type: Optional[str] = None, details: Optional[Dict] = None):
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.details = details or {}
        super().__init__(f"{status_code}: {message}")

class BadRequestError(HTTPError):
    """400 Bad Request."""
    def __init__(self, message: str = 'Bad request', details: Optional[Dict] = None):
        super().__init__(400, message, 'bad_request', details)

class UnauthorizedError(HTTPError):
    """401 Unauthorized."""
    def __init__(self, message: str = 'Unauthorized', details: Optional[Dict] = None):
        super().__init__(401, message, 'unauthorized', details)

class ForbiddenError(HTTPError):
    """403 Forbidden."""
    def __init__(self, message: str = 'Forbidden', details: Optional[Dict] = None):
        super().__init__(403, message, 'forbidden', details)

class NotFoundError(HTTPError):
    """404 Not Found."""
    def __init__(self, message: str = 'Not found', details: Optional[Dict] = None):
        super().__init__(404, message, 'not_found', details)

class ValidationError(HTTPError):
    """422 Unprocessable Entity (validation error)."""
    def __init__(self, message: str = 'Validation failed', details: Optional[Dict] = None):
        super().__init__(422, message, 'validation_error', details)

class InternalServerError(HTTPError):
    """500 Internal Server Error."""
    def __init__(self, message: str = 'Internal server error', details: Optional[Dict] = None):
        super().__init__(500, message, 'internal_error', details)

class BadGatewayError(HTTPError):
    """502 Bad Gateway."""
    def __init__(self, message: str = 'Bad gateway', details: Optional[Dict] = None):
        super().__init__(502, message, 'bad_gateway', details)

# -------------------------------------------------------------------
# Response Utilities
# -------------------------------------------------------------------

def send_response(
    handler: http.server.BaseHTTPRequestHandler,
    status: int = 200,
    content_type: str = JSON_CONTENT_TYPE,
    body: Optional[Union[str, bytes, Dict, list]] = None,
    headers: Optional[Dict[str, str]] = None,
    add_cache_control: bool = True
) -> None:
    """
    Send a standardized HTTP response.
    
    Args:
        handler: HTTP request handler instance
        status: HTTP status code
        content_type: Content-Type header value
        body: Response body (string, bytes, or JSON-serializable object)
        headers: Additional headers to include
        add_cache_control: Whether to add cache control headers
    """
    # Convert body to bytes
    if body is None:
        body_bytes = b''
    elif isinstance(body, (dict, list)):
        body_bytes = json.dumps(body, indent=2).encode(DEFAULT_CHARSET)
    elif isinstance(body, str):
        body_bytes = body.encode(DEFAULT_CHARSET)
    elif isinstance(body, bytes):
        body_bytes = body
    else:
        body_bytes = str(body).encode(DEFAULT_CHARSET)
    
    # Send response
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(body_bytes)))
    
    # Add cache control if requested
    if add_cache_control:
        if status == 200 and 'immutable' in content_type:
            handler.send_header('Cache-Control', 'public, max-age=31536000, immutable')
        else:
            handler.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            handler.send_header('Pragma', 'no-cache')
    
    # Add additional headers
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    
    handler.end_headers()
    
    # Write body if not empty
    if body_bytes:
        handler.wfile.write(body_bytes)

def send_json(
    handler: http.server.BaseHTTPRequestHandler,
    data: Any,
    status: int = 200,
    headers: Optional[Dict[str, str]] = None
) -> None:
    """
    Send a JSON response with standardized formatting.
    
    Args:
        handler: HTTP request handler instance
        data: JSON-serializable data
        status: HTTP status code
        headers: Additional headers
    """
    send_response(
        handler,
        status=status,
        content_type=JSON_CONTENT_TYPE,
        body=data,
        headers=headers,
        add_cache_control=True
    )

def send_error(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    message: str,
    error_type: Optional[str] = None,
    details: Optional[Dict] = None
) -> None:
    """
    Send a standardized error response.
    
    Args:
        handler: HTTP request handler instance
        status: HTTP status code
        message: Human-readable error message
        error_type: Machine-readable error type
        details: Additional error details
    """
    error_data = {
        'error': {
            'code': status,
            'message': message,
            'type': error_type or HTTPStatus(status).phrase.lower().replace(' ', '_'),
            'timestamp': time.time()
        }
    }
    
    if details:
        error_data['error']['details'] = details
    
    send_json(handler, error_data, status=status)

def send_text(
    handler: http.server.BaseHTTPRequestHandler,
    text: str,
    status: int = 200,
    headers: Optional[Dict[str, str]] = None
) -> None:
    """
    Send a plain text response.
    
    Args:
        handler: HTTP request handler instance
        text: Text content
        status: HTTP status code
        headers: Additional headers
    """
    send_response(
        handler,
        status=status,
        content_type=TEXT_CONTENT_TYPE,
        body=text,
        headers=headers,
        add_cache_control=False
    )

def send_html(
    handler: http.server.BaseHTTPRequestHandler,
    html: str,
    status: int = 200,
    headers: Optional[Dict[str, str]] = None
) -> None:
    """
    Send an HTML response.
    
    Args:
        handler: HTTP request handler instance
        html: HTML content
        status: HTTP status code
        headers: Additional headers
    """
    send_response(
        handler,
        status=status,
        content_type=HTML_CONTENT_TYPE,
        body=html,
        headers=headers,
        add_cache_control=False
    )

def redirect(
    handler: http.server.BaseHTTPRequestHandler,
    location: str,
    status: int = 302
) -> None:
    """
    Send a redirect response.
    
    Args:
        handler: HTTP request handler instance
        location: Redirect URL
        status: Redirect status code (302, 301, etc.)
    """
    handler.send_response(status)
    handler.send_header('Location', location)
    handler.send_header('Content-Length', '0')
    handler.end_headers()

# -------------------------------------------------------------------
# Request Utilities
# -------------------------------------------------------------------

def read_json_body(handler: http.server.BaseHTTPRequestHandler, max_size: int) -> Dict:
    """
    Read and parse JSON request body with size limit.
    
    Args:
        handler: HTTP request handler instance
        max_size: Maximum allowed body size in bytes
    
    Returns:
        Parsed JSON as dictionary
    
    Raises:
        BadRequestError: If body exceeds max_size or is invalid JSON
    """
    content_length = int(handler.headers.get('Content-Length', 0))
    
    if content_length > max_size:
        raise BadRequestError(f'Request body too large (max {max_size} bytes)')
    
    if content_length == 0:
        return {}
    
    raw = handler.rfile.read(content_length).decode(DEFAULT_CHARSET)
    
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        raise BadRequestError(f'Invalid JSON: {str(e)}')

# -------------------------------------------------------------------
# Router/Dispatcher
# -------------------------------------------------------------------

class Router:
    """
    Simple HTTP router for request dispatching.
    """
    def __init__(self):
        self.routes = {
            'GET': {},
            'POST': {},
            'PUT': {},
            'DELETE': {},
            'PATCH': {}
        }
    
    def route(self, method: str, path: str) -> Callable:
        """
        Decorator to register a route.
        
        Args:
            method: HTTP method
            path: Route path (can include regex patterns)
        
        Returns:
            Decorator function
        """
        def decorator(func: Callable) -> Callable:
            self.routes[method.upper()][path] = func
            return func
        return decorator
    
    def get(self, path: str) -> Callable:
        """Register a GET route."""
        return self.route('GET', path)
    
    def post(self, path: str) -> Callable:
        """Register a POST route."""
        return self.route('POST', path)
    
    def put(self, path: str) -> Callable:
        """Register a PUT route."""
        return self.route('PUT', path)
    
    def delete(self, path: str) -> Callable:
        """Register a DELETE route."""
        return self.route('DELETE', path)
    
    def patch(self, path: str) -> Callable:
        """Register a PATCH route."""
        return self.route('PATCH', path)
    
    def dispatch(
        self,
        handler: http.server.BaseHTTPRequestHandler,
        method: str,
        path: str
    ) -> bool:
        """
        Dispatch request to registered handler.
        
        Args:
            handler: HTTP request handler instance
            method: HTTP method
            path: Request path
        
        Returns:
            True if route was found and handled, False otherwise
        """
        method = method.upper()
        
        if method not in self.routes:
            return False
        
        # Try exact match first
        if path in self.routes[method]:
            try:
                self.routes[method][path](handler)
                return True
            except HTTPError as e:
                send_error(handler, e.status_code, e.message, e.error_type, e.details)
                return True
            except Exception as e:
                send_error(handler, 500, f'Internal server error: {str(e)}')
                # Log full traceback for debugging
                print(f"Unhandled exception in route {method} {path}:")
                traceback.print_exc()
                return True
        
        # Try regex match (simple pattern matching)
        for route_pattern, route_handler in self.routes[method].items():
            if '*' in route_pattern:
                # Simple wildcard matching
                pattern_parts = route_pattern.split('*')
                if len(pattern_parts) == 2:
                    if path.startswith(pattern_parts[0]) and path.endswith(pattern_parts[1]):
                        try:
                            route_handler(handler)
                            return True
                        except HTTPError as e:
                            send_error(handler, e.status_code, e.message, e.error_type, e.details)
                            return True
                        except Exception as e:
                            send_error(handler, 500, f'Internal server error: {str(e)}')
                            print(f"Unhandled exception in route {method} {route_pattern}:")
                            traceback.print_exc()
                            return True
        
        return False

# -------------------------------------------------------------------
# Middleware/Decorators
# -------------------------------------------------------------------

def require_auth(check_func: Callable) -> Callable:
    """
    Decorator to require authentication.
    
    Args:
        check_func: Function that returns True if request is authenticated
    
    Returns:
        Decorated handler function
    """
    def decorator(handler_func: Callable) -> Callable:
        def wrapper(handler: http.server.BaseHTTPRequestHandler, *args, **kwargs):
            if not check_func(handler):
                raise UnauthorizedError('Authentication required')
            return handler_func(handler, *args, **kwargs)
        return wrapper
    return decorator

def handle_errors(func: Callable) -> Callable:
    """
    Decorator to handle errors in route handlers.
    
    Args:
        func: Route handler function
    
    Returns:
        Wrapped function with error handling
    """
    def wrapper(handler: http.server.BaseHTTPRequestHandler, *args, **kwargs):
        try:
            return func(handler, *args, **kwargs)
        except HTTPError as e:
            send_error(handler, e.status_code, e.message, e.error_type, e.details)
        except Exception as e:
            send_error(handler, 500, f'Internal server error: {str(e)}')
            print(f"Unhandled exception in {func.__name__}:")
            traceback.print_exc()
    return wrapper
