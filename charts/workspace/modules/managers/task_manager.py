"""Task Manager Module - Extracted from server.py

This module contains the ClaudeTaskManager class for managing Claude Code tasks
running in tmux sessions.
"""

import os
import secrets
import json
import time
import subprocess
import threading
from typing import Dict, List, Optional, Any


class ClaudeTaskManager:
    """Manages Claude Code tasks running in tmux sessions"""

    TASKS_DIR = '/home/dev/.claude-tasks'
    TOKEN_FILE = '/home/dev/.claude-tasks/.api-token'

    @staticmethod
    def ensure_tasks_dir():
        os.makedirs(ClaudeTaskManager.TASKS_DIR, mode=0o700, exist_ok=True)

    @staticmethod
    def get_or_create_token():
        ClaudeTaskManager.ensure_tasks_dir()
        if os.path.exists(ClaudeTaskManager.TOKEN_FILE):
            with open(ClaudeTaskManager.TOKEN_FILE, 'r') as f:
                token = f.read().strip()
                if token:
                    return token
        token = secrets.token_urlsafe(36)
        with open(ClaudeTaskManager.TOKEN_FILE, 'w') as f:
            f.write(token)
        os.chmod(ClaudeTaskManager.TOKEN_FILE, 0o600)
        return token

    @staticmethod
    def verify_token(token):
        if not os.path.exists(ClaudeTaskManager.TOKEN_FILE):
            return False
        with open(ClaudeTaskManager.TOKEN_FILE, 'r') as f:
            stored = f.read().strip()
        return secrets.compare_digest(token, stored)

    @staticmethod
    def regenerate_token():
        ClaudeTaskManager.ensure_tasks_dir()
        token = secrets.token_urlsafe(36)
        with open(ClaudeTaskManager.TOKEN_FILE, 'w') as f:
            f.write(token)
        os.chmod(ClaudeTaskManager.TOKEN_FILE, 0o600)
        return token

    @staticmethod
    def _cli_command_for_assistant(assistant: Optional[str], workdir: str) -> str:
        """Return the shell command that launches the selected assistant.
        
        Assistant options:
            - 'claude' (default) — Anthropic Claude Code
            - 'opencode-openrouter' — OpenCode CLI via OpenRouter
            - 'opencode-deepseek' — OpenCode CLI via DeepSeek native API
            - 'kc-harness' — kc-harness against configured Ollama endpoint
        """
        # Note: Import moved inside function to avoid circular imports
        from .utils.helpers import shell_quote
        
        if not assistant or assistant == 'claude':
            return 'claude'
        
        if assistant == 'opencode-openrouter':
            model = os.environ.get('KC_OPENROUTER_MODEL', 'deepseek/deepseek-coder-v3:free')
            return f'opencode --model {shell_quote(f"openrouter/{model}")}'
        
        if assistant == 'opencode-deepseek':
            model = os.environ.get('KC_DEEPSEEK_MODEL', 'deepseek-coder')
            return f'opencode --model {shell_quote(f"deepseek/{model}")}'
        
        if assistant == 'kc-harness':
            model = os.environ.get('KC_HARNESS_MODEL', 'llama3.2:latest')
            return f'kc-harness --model {shell_quote(model)}'
        
        # Fallback to default Claude
        return 'claude'

    @staticmethod
    def create_task(prompt: str, workdir: str = '/home/dev', 
                    source: str = '', assistant: Optional[str] = None) -> Dict[str, Any]:
        """Create a new Claude Code task in a tmux session.
        
        Returns:
            Dict with keys: task_id, status, tmux_session, output_log, error (if failed)
        """
        ClaudeTaskManager.ensure_tasks_dir()
        task_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        session_name = f'claude-{task_id}'
        output_log = os.path.join(ClaudeTaskManager.TASKS_DIR, f'{task_id}.log')
        
        # Get the appropriate CLI command
        cli_cmd = ClaudeTaskManager._cli_command_for_assistant(assistant, workdir)
        
        # Build shell command
        from .utils.helpers import shell_quote
        shell_cmd = f'cd {shell_quote(workdir)} && {cli_cmd}'
        
        # Create tmux session
        tmux_cmd = ['tmux', 'new-session', '-d', '-s', session_name, shell_cmd]
        result = subprocess.run(tmux_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            return {
                'task_id': task_id,
                'status': 'error',
                'error': result.stderr.strip() or 'tmux session creation failed',
                'tmux_session': session_name,
                'output_log': output_log,
            }
        
        # Set up pane logging
        subprocess.run(
            ['tmux', 'pipe-pane', '-t', session_name, 
             f'cat >> {shell_quote(output_log)}'],
            capture_output=True,
        )
        
        # Save task metadata
        task_file = os.path.join(ClaudeTaskManager.TASKS_DIR, f'{task_id}.json')
        task_data = {
            'task_id': task_id,
            'created_at': time.time(),
            'status': 'running',
            'tmux_session': session_name,
            'output_log': output_log,
            'workdir': workdir,
            'prompt': prompt[:200],  # Store truncated prompt for display
            'source': source,
            'assistant': assistant or 'claude',
        }
        
        with open(task_file, 'w') as f:
            json.dump(task_data, f)
        
        # If prompt is provided, send it to tmux
        if prompt.strip():
            # Use buffer for reliable input
            prompt_file = f'/tmp/claude-input-{task_id}'
            with open(prompt_file, 'w') as f:
                f.write(prompt.strip() + '\n')
            
            subprocess.run(['tmux', 'load-buffer', '-b', 'claude-input', prompt_file], 
                          capture_output=True)
            subprocess.run(['tmux', 'paste-buffer', '-b', 'claude-input', '-t', session_name], 
                          capture_output=True)
            subprocess.run(['tmux', 'send-keys', '-t', session_name, 'Enter'], 
                          capture_output=True)
            subprocess.run(['tmux', 'delete-buffer', '-b', 'claude-input'], 
                          capture_output=True)
            os.unlink(prompt_file)
        
        return task_data

    @staticmethod
    def get_task(task_id: str) -> Optional[Dict[str, Any]]:
        """Get task metadata by ID."""
        task_file = os.path.join(ClaudeTaskManager.TASKS_DIR, f'{task_id}.json')
        if not os.path.exists(task_file):
            return None
        
        try:
            with open(task_file, 'r') as f:
                data = json.load(f)
            
            # Check if session is still alive
            result = subprocess.run(
                ['tmux', 'has-session', '-t', data.get('tmux_session', '')],
                capture_output=True,
            )
            data['alive'] = result.returncode == 0
            
            return data
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def list_tasks(limit: int = 50) -> List[Dict[str, Any]]:
        """List recent tasks sorted by creation time."""
        ClaudeTaskManager.ensure_tasks_dir()
        tasks = []
        
        try:
            for filename in os.listdir(ClaudeTaskManager.TASKS_DIR):
                if not filename.endswith('.json'):
                    continue
                
                task_id = filename[:-5]  # Remove .json
                task = ClaudeTaskManager.get_task(task_id)
                if task:
                    tasks.append(task)
        except OSError:
            pass
        
        # Sort by creation time, newest first
        tasks.sort(key=lambda t: t.get('created_at', 0), reverse=True)
        return tasks[:limit]

    @staticmethod
    def kill_task(task_id: str) -> bool:
        """Kill a running task's tmux session."""
        task = ClaudeTaskManager.get_task(task_id)
        if not task:
            return False
        
        session_name = task.get('tmux_session')
        if session_name:
            result = subprocess.run(
                ['tmux', 'kill-session', '-t', session_name],
                capture_output=True,
            )
            if result.returncode == 0:
                # Update task status
                task_file = os.path.join(ClaudeTaskManager.TASKS_DIR, f'{task_id}.json')
                if os.path.exists(task_file):
                    with open(task_file, 'r') as f:
                        data = json.load(f)
                    data['status'] = 'killed'
                    data['killed_at'] = time.time()
                    with open(task_file, 'w') as f:
                        json.dump(data, f)
                return True
        
        return False

    @staticmethod
    def cleanup_old_tasks(max_age_hours: int = 24):
        """Clean up task files older than specified hours."""
        ClaudeTaskManager.ensure_tasks_dir()
        now = time.time()
        cutoff = now - (max_age_hours * 3600)
        
        try:
            for filename in os.listdir(ClaudeTaskManager.TASKS_DIR):
                if not filename.endswith('.json'):
                    continue
                
                filepath = os.path.join(ClaudeTaskManager.TASKS_DIR, filename)
                try:
                    with open(filepath, 'r') as f:
                        data = json.load(f)
                    
                    created_at = data.get('created_at', 0)
                    if created_at < cutoff:
                        # Kill session if still running
                        session_name = data.get('tmux_session')
                        if session_name:
                            subprocess.run(['tmux', 'kill-session', '-t', session_name], 
                                         capture_output=True, stderr=subprocess.DEVNULL)
                        # Remove files
                        os.unlink(filepath)
                        
                        # Also remove log file if it exists
                        log_file = data.get('output_log')
                        if log_file and os.path.exists(log_file):
                            os.unlink(log_file)
                            
                except (json.JSONDecodeError, OSError):
                    # If we can't read it, remove it
                    os.unlink(filepath)
        except OSError:
            pass


# Background cleanup thread
_cleanup_thread = None
_cleanup_stop = threading.Event()

def start_background_cleanup(interval_hours: int = 1, max_age_hours: int = 24):
    """Start background thread for periodic task cleanup."""
    global _cleanup_thread
    
    def cleanup_loop():
        while not _cleanup_stop.is_set():
            ClaudeTaskManager.cleanup_old_tasks(max_age_hours)
            # Sleep for interval hours
            _cleanup_stop.wait(timeout=interval_hours * 3600)
    
    _cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    _cleanup_thread.start()

def stop_background_cleanup():
    """Stop the background cleanup thread."""
    global _cleanup_thread
    if _cleanup_thread:
        _cleanup_stop.set()
        _cleanup_thread.join(timeout=5)
        _cleanup_thread = None