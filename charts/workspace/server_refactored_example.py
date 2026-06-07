#!/usr/bin/env python3
"""
Example refactored server.py section showing migration to new architecture.

This demonstrates how to use the new http_utils, config, and error_utils
modules while maintaining backward compatibility.
"""

import os
import sys
import json
import time

# Import new standardized modules
try:
    from http_utils import Router, send_json, send_error, send_text, redirect
    from http_utils import BadRequestError, UnauthorizedError, NotFoundError
    from config import HTTP_PORT, DEFAULT_MAX_REQUEST_BODY_BYTES, TASKS_DIR
    from config import HOME_DIR, WEBHOOKS_DIR, CRONS_DIR
    from error_utils import handle_errors, validate_required_fields, log_error
except ImportError:
    # Fallback for backward compatibility during migration
    print("Warning: New utilities not available, using legacy patterns")
    # Define minimal compatibility shims
    class Router:
        def __init__(self): pass
        def dispatch(self, *args): return False
    
    def send_json(*args, **kwargs): pass
    def send_error(*args, **kwargs): pass
    def send_text(*args, **kwargs): pass
    def redirect(*args, **kwargs): pass
    
    class BadRequestError(Exception): pass
    class UnauthorizedError(Exception): pass
    class NotFoundError(Exception): pass
    
    def handle_errors(func): return func
    def validate_required_fields(*args, **kwargs): pass
    def log_error(*args, **kwargs): pass
    
    # Fallback constants
    HTTP_PORT = 6080
    DEFAULT_MAX_REQUEST_BODY_BYTES = 1024 * 1024
    TASKS_DIR = '/home/dev/.claude-tasks'
    HOME_DIR = '/home/dev'
    WEBHOOKS_DIR = '/home/dev/.claude-triggers/webhooks'
    CRONS_DIR = '/home/dev/.claude-triggers/crons'

# Import existing modules
import http.server
import subprocess
import threading
import uuid

# Create router instance
router = Router()

# -------------------------------------------------------------------
# Example 1: Health and Metrics Endpoints (Refactored)
# -------------------------------------------------------------------

@router.get("/health")
@router.get("/livez")
@router.get("/healthz")
@handle_errors(default_message="Health check failed")
def handle_health(handler):
    """Health check endpoint."""
    send_text(handler, "ok")

@router.get("/metrics")
@handle_errors(default_message="Failed to get metrics")
def handle_metrics(handler):
    """System metrics endpoint."""
    from server import MetricsCollector  # Import from original module
    
    try:
        metrics = MetricsCollector.get_all_metrics()
        send_json(handler, metrics)
    except Exception as e:
        log_error(e, {"endpoint": "/metrics"})
        raise

# -------------------------------------------------------------------
# Example 2: Task Management Endpoints (Refactored)
# -------------------------------------------------------------------

@router.get("/api/claude/tasks")
@handle_errors(default_message="Failed to list tasks")
def handle_list_tasks(handler):
    """List all Claude tasks."""
    from server import ClaudeTaskManager  # Import from original module
    
    try:
        # Check authentication (example)
        if not handler.check_claude_auth():
            raise UnauthorizedError("Authentication required")
        
        tasks = ClaudeTaskManager.list_tasks()
        send_json(handler, {"tasks": tasks})
    except Exception as e:
        log_error(e, {"endpoint": "/api/claude/tasks"})
        raise

@router.post("/api/claude/tasks")
@handle_errors(default_message="Failed to create task")
def handle_create_task(handler):
    """Create a new Claude task."""
    from server import ClaudeTaskManager  # Import from original module
    from http_utils import read_json_body
    
    try:
        # Check authentication
        if not handler.check_claude_auth():
            raise UnauthorizedError("Authentication required")
        
        # Read and validate request body
        data = read_json_body(handler, DEFAULT_MAX_REQUEST_BODY_BYTES)
        validate_required_fields(data, ["prompt"])
        
        # Extract parameters with defaults
        prompt = data["prompt"]
        workdir = data.get("workdir", HOME_DIR)
        assistant = data.get("assistant", "claude")
        
        # Create task
        task = ClaudeTaskManager.create_task(
            prompt=prompt,
            workdir=workdir,
            assistant=assistant,
            response_url=data.get("response_url"),
            response_secret=data.get("response_secret")
        )
        
        send_json(handler, task, status=201)
    except Exception as e:
        log_error(e, {"endpoint": "/api/claude/tasks", "action": "create"})
        raise

@router.get("/api/claude/tasks/*")
@handle_errors(default_message="Failed to get task")
def handle_get_task(handler):
    """Get a specific task by ID."""
    from server import ClaudeTaskManager  # Import from original module
    
    try:
        # Extract task ID from path
        path_parts = handler.path.split('/')
        task_id = path_parts[-1] if path_parts else ""
        
        if not task_id:
            raise BadRequestError("Task ID required")
        
        # Check authentication
        if not handler.check_claude_auth():
            raise UnauthorizedError("Authentication required")
        
        # Get task
        task = ClaudeTaskManager.get_task(task_id)
        if not task:
            raise NotFoundError(f"Task {task_id} not found")
        
        send_json(handler, task)
    except Exception as e:
        log_error(e, {"endpoint": "/api/claude/tasks/*", "task_id": task_id})
        raise

# -------------------------------------------------------------------
# Example 3: Webhook Management Endpoints (Refactored)
# -------------------------------------------------------------------

@router.get("/api/webhooks")
@handle_errors(default_message="Failed to list webhooks")
def handle_list_webhooks(handler):
    """List all webhook configurations."""
    from server import WebhookManager  # Import from original module
    
    try:
        # Check authentication
        if not handler.check_claude_auth():
            raise UnauthorizedError("Authentication required")
        
        webhooks = WebhookManager.list_webhooks()
        send_json(handler, {"webhooks": webhooks})
    except Exception as e:
        log_error(e, {"endpoint": "/api/webhooks"})
        raise

@router.post("/api/webhooks")
@handle_errors(default_message="Failed to create webhook")
def handle_create_webhook(handler):
    """Create a new webhook configuration."""
    from server import WebhookManager  # Import from original module
    from http_utils import read_json_body
    
    try:
        # Check authentication
        if not handler.check_claude_auth():
            raise UnauthorizedError("Authentication required")
        
        # Read and validate request body
        data = read_json_body(handler, DEFAULT_MAX_REQUEST_BODY_BYTES)
        validate_required_fields(data, ["id", "prompt_template"])
        
        # Create webhook
        webhook, error = WebhookManager.create_or_update(data)
        if error:
            raise BadRequestError(error)
        
        send_json(handler, webhook, status=201)
    except Exception as e:
        log_error(e, {"endpoint": "/api/webhooks", "action": "create"})
        raise

# -------------------------------------------------------------------
# Example 4: Mixed Legacy and New Pattern Handler
# -------------------------------------------------------------------

class RefactoredBrowserHandler(http.server.SimpleHTTPRequestHandler):
    """Example handler showing mixed legacy and new patterns."""
    
    # Legacy methods for backward compatibility
    def check_claude_auth(self):
        """Legacy authentication check (from original server.py)."""
        # Simplified example - real implementation would check tokens
        auth_header = self.headers.get('Authorization', '')
        return auth_header.startswith('Bearer ')
    
    def log_message(self, fmt, *args):
        """Legacy logging (from original server.py)."""
        sys.stderr.write("[server] %s - %s\n" % (self.address_string(), fmt % args))
    
    def do_GET(self):
        """Handle GET requests with mixed legacy/new routing."""
        # Normalize path
        path = self.path.split('?')[0]
        
        # Try new router first
        if router.dispatch(self, "GET", path):
            return
        
        # Fall back to legacy routing for unmigrated endpoints
        self.handle_legacy_get(path)
    
    def do_POST(self):
        """Handle POST requests with mixed legacy/new routing."""
        # Normalize path
        path = self.path.split('?')[0]
        
        # Try new router first
        if router.dispatch(self, "POST", path):
            return
        
        # Fall back to legacy routing for unmigrated endpoints
        self.handle_legacy_post(path)
    
    def handle_legacy_get(self, path):
        """Legacy GET routing (example)."""
        if path == "/api/workspace/dirs":
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            # Call legacy function
            from server import WorkspaceManager
            dirs = WorkspaceManager.list_dirs()
            self.wfile.write(json.dumps(dirs).encode('utf-8'))
        elif path.startswith("/api/memory/"):
            # Another legacy endpoint
            self.handle_memory_endpoint(path)
        else:
            # Final fallback
            self.send_response(404)
            self.end_headers()
    
    def handle_legacy_post(self, path):
        """Legacy POST routing (example)."""
        if path == "/api/claude/auth/token":
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            # Call legacy function
            from server import ClaudeTaskManager
            token = ClaudeTaskManager.get_or_create_token()
            self.wfile.write(json.dumps({"token": token}).encode('utf-8'))
        else:
            # Final fallback
            self.send_response(404)
            self.end_headers()
    
    def handle_memory_endpoint(self, path):
        """Example of gradually refactoring a complex endpoint."""
        try:
            # Start using new error handling even in legacy code
            if not self.check_claude_auth():
                send_error(self, 401, "Authentication required")
                return
            
            # Rest of legacy implementation...
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
            
        except Exception as e:
            # Use new error logging even in legacy code
            log_error(e, {"path": path})
            send_error(self, 500, "Internal server error")

# -------------------------------------------------------------------
# Main Function
# -------------------------------------------------------------------

def main():
    """Main function showing server startup with new architecture."""
    print(f"Starting server on port {HTTP_PORT}")
    print(f"Using configuration from {__file__}")
    
    # Start server
    with http.server.ThreadingHTTPServer(("", HTTP_PORT), RefactoredBrowserHandler) as httpd:
        print(f"Server listening on port {HTTP_PORT}")
        print(f"Available routes:")
        for method, routes in router.routes.items():
            for route in routes:
                print(f"  {method} {route}")
        
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            httpd.shutdown()

if __name__ == "__main__":
    main()
