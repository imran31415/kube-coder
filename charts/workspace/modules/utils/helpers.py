#!/usr/bin/env python3
"""
Utility functions for the kube-coder server.
"""

import re
import shlex

# Strip ANSI escape sequences (CSI, OSC, single-char) from terminal output
# captured via `tmux pipe-pane`, so the dashboard chat view stays readable.
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;?]*[ -/]*[@-~]'   # CSI
    r'|\x1b\][^\x07]*\x07'          # OSC ... BEL
    r'|\x1b[NOPYZ\\^_=>78<]'        # single-char escapes
)


def strip_ansi(text):
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub('', text)


def detect_waiting_for_input(output):
    """Detect common patterns indicating waiting for human input.
    
    Returns:
        tuple: (is_waiting, last_prompt_line)
    """
    if not output or not output.strip():
        return False, ""
    
    lines = output.strip().split('\n')
    if not lines:
        return False, ""
    
    # Get the last few non-empty lines for pattern analysis
    last_lines = []
    for line in reversed(lines):
        if line.strip():
            last_lines.append(line.strip())
        if len(last_lines) >= 3:  # Check last 3 non-empty lines
            break
    
    if not last_lines:
        return False, ""
    
    # Patterns that typically indicate waiting for input
    waiting_patterns = [
        r'.*[?]\s*$',                    # Questions ending with ?
        r'.*>\s*$',                      # Shell-like prompts ending with >
        r'.*:\s*$',                      # Prompts ending with :
        r'.*Press\s+(any\s+)?key.*',     # "Press any key" messages
        r'.*Enter\s+your\s+(choice|input|answer).*', # "Enter your choice"
        r'.*Please\s+(provide|enter|type|input).*',  # "Please provide"
        r'.*Waiting\s+for.*input.*',     # "Waiting for input"
        r'.*Continue\?\s*(\(y/n\))?\s*$', # "Continue? (y/n)"
        r'.*\(y/n\)\s*$',               # Simple (y/n) prompts
        r'.*\[.*\]\?\s*$',              # Bracketed choice prompts like [y/N]?
        r'.*\s+\$\s*$',                 # Command prompts ending with $
        r'.*#\s*$',                     # Root prompts ending with #
        r'.*Select\s+an?\s+option.*',   # "Select an option"
        r'.*Choose\s+(from|an?).*',     # "Choose from" or "Choose an"
        r'.*Which\s+.*\?.*',           # "Which option?" type questions
    ]
    
    # Check each of the last few lines
    for line in last_lines:
        for pattern in waiting_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                # Return the line that matched as the prompt
                return True, line
    
    return False, ""


def shell_quote(s):
    """Quote a string for safe use in a shell command."""
    return shlex.quote(s)
