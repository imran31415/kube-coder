#!/usr/bin/env python3
# harness_test.py — drive kc-harness end-to-end with a fixed prompt matrix
# and report which prompts produced a real tool invocation.
#
# Usage inside an imran-style workspace pod:
#   python3 /tmp/browser/harness_test.py             # current model only
#   python3 /tmp/browser/harness_test.py --sweep     # all candidate models
#
# Exits 0 on success (≥7/8 prompts produced a real tool use, no XML leak).
# Exits 1 on failure — caller should treat that as "do not ship yet".

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().with_name("harness.py")

# 8 prompts mirroring the matrix used during the kc-harness design session,
# so improvements/regressions are directly comparable against earlier
# OpenCode results.
PROMPTS = [
    "list files in current dir",
    "what files are here?",
    "run ls -la",
    "show me the directory contents",
    "ls",
    "what is in this folder",
    "use bash to list files",
    "run a shell command to see files",
]

# Models on the GPU droplet that are worth trying for kc-harness's narrow
# tool surface. Order: cheapest first so a sweep aborts early on small
# models that already work well enough.
SWEEP_MODELS = [
    "qwen3:32b-q4_K_M",
    "qwen3-coder:30b",
    "qwen2.5-coder:32b-instruct-q4_K_M",
    "qwen2.5:14b-instruct-q4_K_M",
    "qwen3.6:35b",
    "llama3.1:70b-instruct-q4_K_M",
]


def run_one(prompt: str, model: str | None, timeout: int = 240) -> dict:
    """Returns {'tool_used': bool, 'xml_leak': bool, 'final': str, 'raw': str}."""
    env = dict(os.environ)
    if model:
        env["KC_HARNESS_MODEL"] = model
    try:
        proc = subprocess.run(
            [sys.executable, str(HARNESS), "--once", prompt],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"tool_used": False, "xml_leak": False, "final": "(timeout)", "raw": ""}

    tool_used = False
    xml_leak = False
    final_text = ""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "result":
            final_text = ev.get("result") or ""
        content = ev.get("message", {}).get("content") or []
        for block in content if isinstance(content, list) else []:
            btype = block.get("type")
            if btype == "tool_use":
                tool_used = True
            elif btype == "text":
                txt = block.get("text") or ""
                if "<function=" in txt or "<tool_call>" in txt:
                    xml_leak = True
    # Also check the final answer text for XML leaks (model giving up
    # with the raw block still in it).
    if "<function=" in final_text or "<tool_call>" in final_text:
        xml_leak = True
    return {
        "tool_used": tool_used,
        "xml_leak": xml_leak,
        "final": final_text,
        "raw": proc.stdout + proc.stderr,
    }


def run_matrix(model: str | None, label: str) -> int:
    """Run all prompts once, print a one-line summary per prompt, and
    return the count of prompts that produced a real tool invocation."""
    pass_count = 0
    leak_count = 0
    print(f"\n=== {label} ===")
    for p in PROMPTS:
        res = run_one(p, model)
        flag = "[PASS]" if res["tool_used"] and not res["xml_leak"] else (
            "[XML] " if res["xml_leak"] else "[FAIL]"
        )
        if res["tool_used"] and not res["xml_leak"]:
            pass_count += 1
        if res["xml_leak"]:
            leak_count += 1
        # Trim final answer for readability.
        snippet = res["final"].splitlines()[0][:80] if res["final"] else "(empty)"
        print(f"  {flag} {p[:48]:48s} → {snippet}")
    print(f"  total: {pass_count}/{len(PROMPTS)} passed, {leak_count} xml leaks")
    return pass_count


def main():
    if not HARNESS.exists():
        sys.exit(f"harness.py not found next to this script ({HARNESS})")
    if not shutil.which("python3"):
        sys.exit("python3 not on PATH")

    base = os.environ.get("KC_FALLBACK_BASE_URL") or os.environ.get(
        "KC_HARNESS_BASE_URL", ""
    )
    if not base:
        sys.exit(
            "no upstream configured — set KC_HARNESS_BASE_URL or KC_FALLBACK_BASE_URL"
        )

    if "--sweep" in sys.argv[1:]:
        scores = []
        for m in SWEEP_MODELS:
            n = run_matrix(m, f"model = {m}")
            scores.append((n, m))
        scores.sort(key=lambda x: (-x[0], x[1]))
        print("\n=== ranking ===")
        for n, m in scores:
            print(f"  {n}/{len(PROMPTS)}   {m}")
        # Exit 0 iff the best model gets ≥7/8.
        sys.exit(0 if scores and scores[0][0] >= 7 else 1)

    n = run_matrix(None, f"model = {os.environ.get('KC_HARNESS_MODEL') or os.environ.get('KC_FALLBACK_MODEL') or '(default)'}")
    sys.exit(0 if n >= 7 else 1)


if __name__ == "__main__":
    main()
