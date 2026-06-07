#!/usr/bin/env python3
"""
Configuration constants for kube-coder.

Extracts magic numbers and hardcoded values to a centralized location
for consistent management across the codebase.
"""

import os
from typing import Dict, Any

# -------------------------------------------------------------------
# Port Constants
# -------------------------------------------------------------------

# Main service ports
HTTP_PORT = 6080  # Main browser/API server
CONTROLLER_PORT = 8080  # Controller API server
VSCODE_PORT = 8080  # VS Code server
TERMINAL_PORT = 7681  # ttyd terminal
VNC_WEBSOCKIFY_PORT = 6081  # noVNC websockify
VNC_SERVER_PORT = 5900  # x11vnc server
DOCKER_PORT = 2376  # Docker daemon (dind)
SSH_PORT = 22  # SSH server

# Internal ports that shouldn't be proxied
INTERNAL_PORTS = frozenset({
    SSH_PORT,
    DOCKER_PORT,
    VNC_SERVER_PORT,
    VNC_WEBSOCKIFY_PORT,
    TERMINAL_PORT,
    VSCODE_PORT,
    HTTP_PORT
})

# -------------------------------------------------------------------
# File and Path Constants
# -------------------------------------------------------------------

# Home directory and workspace paths
HOME_DIR = '/home/dev'
TASKS_DIR = os.path.join(HOME_DIR, '.claude-tasks')
WEBHOOKS_DIR = os.path.join(HOME_DIR, '.claude-triggers', 'webhooks')
CRONS_DIR = os.path.join(HOME_DIR, '.claude-triggers', 'crons')
MEMORY_DB_PATH = os.path.join(HOME_DIR, '.claude-memory', 'memory.db')

# SSH and config directories
SSH_DIR = os.path.join(HOME_DIR, '.ssh')
GH_CONFIG_DIR = os.path.join(HOME_DIR, '.config', 'gh')

# Token files
API_TOKEN_FILE = os.path.join(TASKS_DIR, '.api-token')

# -------------------------------------------------------------------
# HTTP Request Constants
# -------------------------------------------------------------------

# Request size limits (bytes)
DEFAULT_MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024  # 1 MB
CONTROLLER_MAX_REQUEST_BODY_BYTES = 64 * 1024  # 64 KB
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB

# Timeouts (seconds)
STREAM_MAX_SECONDS = 1800  # 30 minutes
KUBECTL_TIMEOUT = 15
PROMETHEUS_TIMEOUT = 8
COMPLETION_HOOK_TIMEOUT = 10

# Replay protection
REPLAY_CACHE_CAPACITY = 1024
REPLAY_CACHE_TTL_SECONDS = 300  # 5 minutes
TIMESTAMP_TOLERANCE = 300  # 5 minutes (Slack/Stripe signature tolerance)

# -------------------------------------------------------------------
# Resource Constants
# -------------------------------------------------------------------

# Memory units conversion
MEMORY_UNITS = [
    ('Ki', 1024), ('Mi', 1024**2), ('Gi', 1024**3), 
    ('Ti', 1024**4), ('Pi', 1024**5),
    ('K', 1e3), ('M', 1e6), ('G', 1e9), 
    ('T', 1e12), ('P', 1e15), ('k', 1e3)
]

# Default resource requests and limits
DEFAULT_RESOURCES = {
    'requests': {
        'cpu': '1',
        'memory': '2Gi'
    },
    'limits': {
        'cpu': '2',
        'memory': '6Gi'
    }
}

# Controller default resources
CONTROLLER_RESOURCES = {
    'requests': {
        'cpu': '50m',
        'memory': '64Mi'
    },
    'limits': {
        'cpu': '200m',
        'memory': '256Mi'
    }
}

# dind (Docker in Docker) resources
DIND_RESOURCES = {
    'requests': {
        'cpu': '100m',
        'memory': '256Mi'
    },
    'limits': {
        'cpu': '2',
        'memory': '4Gi'
    }
}

# SSH sidecar resources
SSH_RESOURCES = {
    'requests': {
        'cpu': '50m',
        'memory': '64Mi'
    },
    'limits': {
        'cpu': '200m',
        'memory': '256Mi'
    }
}

# -------------------------------------------------------------------
# Monitoring and Alerting Constants
# -------------------------------------------------------------------

# Alert thresholds (percentage)
ALERT_THRESHOLDS = {
    'cpu': {'warning': 70, 'critical': 90},
    'memory': {'warning': 80, 'critical': 95},
    'disk': {'warning': 80, 'critical': 90}
}

# Insights configuration
INSIGHTS_WINDOW_SECONDS = 21600  # 6 hours
INSIGHTS_IDLE_CPU_CORES = 0.05  # 50m CPU considered idle

# Cost calculation defaults
HOURS_PER_MONTH = 730.0  # 730 hours per month (24 * 365 / 12)
DEFAULT_COST_CPU_CORE_HOUR = 0.0082  # $0.0082 per core-hour
DEFAULT_COST_MEM_GB_HOUR = 0.0041  # $0.0041 per GB-hour
DEFAULT_COST_STORAGE_GB_MONTH = 0.10  # $0.10 per GB-month

# -------------------------------------------------------------------
# Task Management Constants
# -------------------------------------------------------------------

# Task statuses
TASK_STATUS_RUNNING = 'running'
TASK_STATUS_WAITING_FOR_INPUT = 'waiting-for-input'
TASK_STATUS_COMPLETED = 'completed'
TASK_STATUS_KILLED = 'killed'
TASK_STATUS_ERROR = 'error'

# Task output limits
TASK_OUTPUT_TAIL_LINES = 200
TASK_RECENT_OUTPUT_LINES = 50
TASK_CAPTURE_PANE_LINES = 200

# Task creation delays (seconds)
TASK_PROMPT_SEND_DELAY = 3  # Wait for claude to initialize

# -------------------------------------------------------------------
# Memory System Constants
# -------------------------------------------------------------------

# Memory injection defaults
MEMORY_INJECTION_DEFAULTS = {
    'enabled': True,
    'top_k': 8,
    'min_score': 0.30,
    'max_chars': 4096
}

# Memory consolidation
MEMORY_CONSOLIDATION_DEFAULTS = {
    'enabled': False,
    'interval_hours': 6
}

# -------------------------------------------------------------------
# Webhook and Cron Constants
# -------------------------------------------------------------------

# ID validation patterns
WEBHOOK_ID_PATTERN = r'^[a-zA-Z0-9_-]{1,64}$'
CRON_ID_PATTERN = r'^[a-z0-9-]{1,40}$'  # Tighter for k8s names

# Schedule validation
CRON_FIELD_PATTERN = r'[0-9*/,-]+'
CRON_SCHEDULE_PATTERN = (
    r'^@(yearly|annually|monthly|weekly|daily|hourly)$|'
    r'^' + r'\s+'.join([CRON_FIELD_PATTERN] * 5) + r'$'
)

# Timezone validation
TIMEZONE_PATTERN = r'^[A-Za-z0-9_/+\-]{1,64}$'

# Webhook providers
WEBHOOK_PROVIDERS = ('generic', 'github', 'slack', 'stripe')

# Interpolation modes
INTERPOLATION_MODES = ('attach', 'interpolate')

# Signature algorithms
SIGNATURE_ALGORITHMS = ('sha256', 'sha1')

# -------------------------------------------------------------------
# Security Constants
# -------------------------------------------------------------------

# Default security settings
DEFAULT_SECURITY_CONTEXT = {
    'runAsNonRoot': True,
    'runAsUser': 1000,
    'runAsGroup': 1000,
    'fsGroup': 1000,
    'seccompProfile': {
        'type': 'RuntimeDefault'
    }
}

# Container security context
CONTAINER_SECURITY_CONTEXT = {
    'allowPrivilegeEscalation': False,
    'capabilities': {
        'drop': ['ALL']
    },
    'seccompProfile': {
        'type': 'RuntimeDefault'
    }
}

# -------------------------------------------------------------------
# Environment Variable Defaults
# -------------------------------------------------------------------

def get_env_defaults() -> Dict[str, Any]:
    """Get default values for environment variables."""
    return {
        'READONLY_MODE': 'false',
        'AUTH_MODE': 'basic',
        'DEMO_SHOW_ALL': 'false',
        'TRUSTED_PROXY': 'true',
        'ALLOW_INTERNAL_HOOKS': 'false',
        'MAX_REQUEST_BODY_BYTES': str(DEFAULT_MAX_REQUEST_BODY_BYTES),
        'STREAM_MAX_SECONDS': str(STREAM_MAX_SECONDS),
        'WORKSPACE_PREFIX': 'ws-',
        'CONTROLLER_PORT': str(CONTROLLER_PORT),
        'KUBECTL_TIMEOUT': str(KUBECTL_TIMEOUT),
        'PROM_TIMEOUT': str(PROMETHEUS_TIMEOUT),
        'COST_CPU_CORE_HOUR': str(DEFAULT_COST_CPU_CORE_HOUR),
        'COST_MEM_GB_HOUR': str(DEFAULT_COST_MEM_GB_HOUR),
        'COST_STORAGE_GB_MONTH': str(DEFAULT_COST_STORAGE_GB_MONTH),
        'INSIGHTS_WINDOW_SECONDS': str(INSIGHTS_WINDOW_SECONDS),
        'INSIGHTS_IDLE_CPU_CORES': str(INSIGHTS_IDLE_CPU_CORES),
    }

# -------------------------------------------------------------------
# Helper Functions
# -------------------------------------------------------------------

def parse_cpu_quantity(quantity: str) -> float:
    """
    Parse Kubernetes CPU quantity to cores.
    
    Examples:
        '2' -> 2.0
        '500m' -> 0.5
        '0.1' -> 0.1
    """
    if not quantity:
        return 0.0
    
    quantity = str(quantity).strip()
    if quantity.endswith('m'):
        return float(quantity[:-1]) / 1000.0
    return float(quantity)

def parse_memory_quantity(quantity: str) -> int:
    """
    Parse Kubernetes memory quantity to bytes.
    
    Examples:
        '6Gi' -> 6 * 1024**3
        '512Mi' -> 512 * 1024**2
        '1000000' -> 1000000
    """
    if not quantity:
        return 0
    
    quantity = str(quantity).strip()
    
    # Try binary units first (Ki, Mi, Gi, Ti, Pi)
    for unit, multiplier in MEMORY_UNITS:
        if quantity.endswith(unit):
            return int(float(quantity[:-len(unit)]) * multiplier)
    
    # No unit, assume bytes
    return int(float(quantity))

def format_bytes(bytes_val: int) -> str:
    """
    Format bytes to human-readable string.
    
    Examples:
        1024 -> '1.0 KB'
        1048576 -> '1.0 MB'
        1073741824 -> '1.0 GB'
    """
    if bytes_val >= 1024 ** 3:  # GB
        return f'{bytes_val / (1024 ** 3):.1f} GB'
    elif bytes_val >= 1024 ** 2:  # MB
        return f'{bytes_val / (1024 ** 2):.1f} MB'
    elif bytes_val >= 1024:  # KB
        return f'{bytes_val / 1024:.1f} KB'
    else:
        return f'{bytes_val} B'

def format_duration(seconds: float) -> str:
    """
    Format duration to human-readable string.
    
    Examples:
        3600 -> '1.0h'
        90 -> '1.5m'
        7200 -> '2h'
    """
    hours = seconds / 3600.0
    if hours < 1:
        return f'{seconds / 60:.0f}m'
    elif hours < 1.5:
        return f'{hours:.1f}h'
    else:
        return f'{hours:.0f}h'
