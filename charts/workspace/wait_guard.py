#!/usr/bin/env python3
"""Turn-boundary guard for Hypervisor/CTO turns (issues #747, #780).

Blocks the three tool shapes that lose work to the per-turn process model:
foreground blocking waits in Bash (#747), background subagents launched with
the Agent tool (#780), and Monitor watchers (#378's shape, now denied rather
than merely mourned).

Two consumers, ONE definition of what each shape is:

  * as a library — ``hypervisor_session`` imports ``foreground_wait_reason``
    to track waits that happened and open the next turn with a corrective
    notice.
  * as a hook — ``python3 wait_guard.py`` is registered as a ``PreToolUse``
    hook on the Hypervisor's ``claude -p`` invocation (inline ``--settings``,
    so it is scoped to chat turns and never touches the user's terminal or a
    Build task). It DENIES the call before it runs.

The notice alone was not a fix. It fires after the chat has already been
frozen for the length of the wait, which is the damage; and a turn killed by
its own timeout never reaches the code that would have recorded anything, so
the worst case is also the silent one. Prohibiting it in the preamble is
advice the model is free to reason its way around — and #747 exists precisely
because it did: it correctly learned "background waiters die at the turn
boundary" and concluded "so wait in the foreground instead".

Denying the tool call is the only version of this that is not advisory.

Fail OPEN, always: any unexpected input, any internal error, and the hook
exits 0 allowing the call. A guard that crashes closed would take the whole
chat down with it, which is far worse than the wait it was meant to prevent.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# A short `sleep 2` to let a dev server bind is normal and must stay allowed,
# so a bare sleep only counts from here up; anything shorter counts only inside
# a poll loop, where the loop is the wait rather than the individual sleep.
FG_SLEEP_SECONDS = 60

# Deliberately NOT a signal: a long Bash `timeout`. #747 proposed it, but
# checked against the real session in that report it produced no true positives
# the command shapes below had not already caught, and it fired on
# `make python-tests` — a 150s test suite under a generous ceiling, which is
# long WORK, not a wait. This guard BLOCKS, so a false positive does not merely
# cost the notice its credibility, it stops honest work from running. The
# detector under-reports on purpose: a missed wait costs one turn, a wrong
# denial costs the user their command.

# Each entry is a blocking wait on EXTERNAL work, not merely a slow command.
# `--follow` is absent for the same reason: `git log --follow` is common and
# innocent, and blocking it would be a bug.
FG_WAIT_PATTERNS = (
    ('gh run watch', re.compile(r'\bgh\s+run\s+watch\b', re.I)),
    ('gh --watch', re.compile(r'\bgh\b[^|;&]*\s--watch\b', re.I)),
    ('kubectl wait', re.compile(r'\bkubectl\s+wait\b', re.I)),
    ('docker wait', re.compile(r'\bdocker\s+wait\b', re.I)),
    ('--wait flag', re.compile(r'\b(?:helm|argocd|flux)\b[^|;&]*\s--wait\b', re.I)),
)
_FG_SLEEP_RE = re.compile(r'\bsleep\s+(\d+(?:\.\d+)?)')
_FG_LOOP_RE = re.compile(r'\b(?:for|while|until)\b')


def foreground_wait_reason(tool_input: Any) -> str:
    """Why this Bash call is a blocking wait — '' when it isn't."""
    d = tool_input if isinstance(tool_input, dict) else {}
    if d.get('run_in_background'):
        return ''  # the #378 lost-watcher path already owns that shape
    cmd = ' '.join(str(d.get('command') or '').split())
    if not cmd:
        return ''
    for label, rx in FG_WAIT_PATTERNS:
        if rx.search(cmd):
            return label
    longest = 0.0
    for m in _FG_SLEEP_RE.finditer(cmd):
        try:
            longest = max(longest, float(m.group(1)))
        except ValueError:
            continue
    if longest:
        if _FG_LOOP_RE.search(cmd):
            return 'poll loop with sleep'
        if longest >= FG_SLEEP_SECONDS:
            return f'sleep {longest:g}s'
    return ''


def denial_reason(reason: str) -> str:
    """What the agent is told when the call is blocked.

    Has to do two jobs at once: refuse, and leave the agent with a concrete
    next action. A bare refusal would just push it to the next workaround —
    which is how #747 happened in the first place.
    """
    return (
        f'Blocked: this is a blocking wait ({reason}) inside a chat turn. '
        'Waiting this way freezes the chat for its whole duration — the user '
        'cannot interject and sees only an unexplained spinner — and it spends '
        'the turn timeout, so a wait that outlasts it kills the turn and loses '
        'the result you were waiting for too. That background waiters die at '
        'the turn boundary is NOT a reason to wait in the foreground instead; '
        'it is a reason not to wait inside a turn at all. '
        'Do this instead: arm a runner-owned watcher with the dashboard '
        '`watch` tool (kind task / command / file) and END your turn. The '
        'runner keeps polling after the turn is over and posts the outcome '
        'into this chat as a new message, so nothing is lost and the user '
        'stays free to talk to you meanwhile. '
        'If you only need the CURRENT state, run the same check WITHOUT its '
        'watch/wait/sleep (e.g. `gh pr checks <n>` rather than '
        '`gh pr checks <n> --watch`) — a one-shot read is fine.'
    )


# Background subagents (issue #780). The built-in Agent tool runs its subagent
# in the BACKGROUND unless run_in_background is explicitly false — and a
# background subagent is an in-process child of the turn's headless CLI, so it
# is killed the moment the turn ends, taking its work and the tokens it spent.
# This is the loss #378 documented for Bash/Monitor, at much higher stakes: a
# fanned-out coding subagent represents minutes-to-hours of work, and the agent
# that armed it ends its turn precisely BECAUSE it expects a completion
# notification that can never arrive. 'Task' is the same tool's older name;
# matching both costs nothing (an unknown name falls through and allows).
AGENT_TOOLS = ('Agent', 'Task')


def background_agent_reason(tool_input: Any) -> str:
    """Why this Agent call dies at the turn boundary — '' when it won't."""
    d = tool_input if isinstance(tool_input, dict) else {}
    if d.get('run_in_background') is False:
        return ''  # foreground: the turn waits for it, nothing crosses the boundary
    return 'background subagent (the Agent tool backgrounds by default)'


def agent_denial_reason(reason: str) -> str:
    return (
        f'Blocked: this would launch a {reason}, and in this chat each turn '
        'runs in a fresh headless CLI process — a background subagent is '
        'KILLED the moment the turn ends, losing its work and the tokens it '
        'spent, and the completion notification you would wait for can never '
        'arrive. Pick by duration instead: for short work you will collect '
        'and use THIS turn, re-issue the same Agent call with '
        'run_in_background: false — several foreground Agent calls in ONE '
        'message still fan out in parallel, and the turn waits for all of '
        'them. For anything minutes-plus (coding, builds, reviews), use the '
        'dashboard create_task tool instead (tmux-backed, survives turns; '
        'isolate: true for parallel work on one repo), arm a `watch` of kind '
        '"task" on each task id, and END your turn — the runner posts each '
        'outcome into this chat as a new message.'
    )


def monitor_denial_reason() -> str:
    return (
        'Blocked: Monitor dies at the turn boundary in this chat — each turn '
        'runs in a fresh headless CLI process, so the condition\'s outcome '
        'could never reach you; a later "no completion record" notification '
        'would be the only trace. Arm a runner-owned watcher instead with the '
        'dashboard `watch` tool (kind task / command / file) and END your '
        'turn: the runner keeps polling after the turn is over and posts the '
        'outcome into this chat as a new message.'
    )


def _deny(reason_text: str) -> None:
    json.dump({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': reason_text,
    }}, sys.stdout)
    sys.stdout.write('\n')


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # unreadable input is not grounds to block the agent
    try:
        name = payload.get('tool_name') or ''
        if name == 'Bash':
            reason = foreground_wait_reason(payload.get('tool_input'))
            if reason:
                _deny(denial_reason(reason))
        elif name in AGENT_TOOLS:
            reason = background_agent_reason(payload.get('tool_input'))
            if reason:
                _deny(agent_denial_reason(reason))
        elif name == 'Monitor':
            _deny(monitor_denial_reason())
    except Exception:
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
