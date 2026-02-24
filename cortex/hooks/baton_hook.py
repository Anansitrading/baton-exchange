#!/usr/bin/env python3
"""Baton Relay Hook — SessionStart context injection.

Fires on Claude Code SessionStart. Calls HyperVisa baton endpoint
to synthesize a compressed baton, then injects it into context
via the additionalContext JSON response field.

This is the READ side of the baton relay loop.
The WRITE side is in compact_hook.py (record_to_cxdb).
"""

import json
import os
import re
import subprocess
import sys
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[baton-hook] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

HYPERVISA_API = "http://localhost:8042/api/hypervisa"
BATON_STATE_DIR = Path.home() / ".cortex" / "baton"


def _detect_project_name(cwd: str) -> str | None:
    """Detect project name from git remote or directory basename."""
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, cwd=cwd,
        )
        if r.returncode == 0:
            m = re.search(r"[/:]([^/:]+?)(?:\.git)?$", r.stdout.strip())
            if m:
                return m.group(1)
    except Exception:
        pass
    basename = Path(cwd).name
    # Skip home directory
    if basename == "devuser" or basename == "home":
        return None
    return basename


def _cache_baton(baton: dict, project: str) -> None:
    """Write baton to cache file so statusline can read it."""
    try:
        BATON_STATE_DIR.mkdir(parents=True, exist_ok=True)
        baton["project"] = project
        # Write per-project cache (statusline reads this)
        per_project = BATON_STATE_DIR / f"baton-{project}.json"
        per_project.write_text(json.dumps(baton))
        # Also write global last-baton for backwards compat
        global_cache = BATON_STATE_DIR / "last-baton.json"
        global_cache.write_text(json.dumps(baton))
    except Exception as e:
        logger.warning(f"Failed to cache baton: {e}")


def _write_inject_state(project: str, success: bool, chars: int = 0) -> None:
    """Write injection telemetry for statusline."""
    try:
        BATON_STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_path = BATON_STATE_DIR / "last-inject.json"
        state_path.write_text(json.dumps({
            "project": project,
            "success": success,
            "timestamp": __import__("time").time(),
            "chars": chars,
            "est_tokens": chars // 4 if chars else 0,
        }))
    except Exception as e:
        logger.warning(f"Failed to write inject state: {e}")


def _call_baton_api(project: str, session_id: str, cwd: str) -> dict | None:
    """Call HyperVisa baton synthesis endpoint."""
    try:
        import urllib.request

        payload = json.dumps({
            "project": project,
            "session_id": session_id,
            "cwd": cwd,
            "compression": "normal",
        }).encode()

        req = urllib.request.Request(
            f"{HYPERVISA_API}/baton",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                return json.loads(resp.read())
    except Exception as e:
        logger.warning(f"Baton API call failed: {e}")
    return None


def _format_baton_context(baton: dict) -> str:
    """Format baton dict into concise context injection text."""
    lines = ["## Baton Relay — Session Context"]

    purpose = baton.get("purpose", "")
    if purpose:
        lines.append(f"**Purpose:** {purpose}")

    persistence = baton.get("persistence", {})
    if persistence.get("in_progress"):
        lines.append(f"**In Progress:** {persistence['in_progress']}")
    if persistence.get("completed"):
        completed = persistence["completed"]
        if isinstance(completed, list):
            lines.append(f"**Completed:** {', '.join(completed[:5])}")
    if persistence.get("next"):
        nexts = persistence["next"]
        if isinstance(nexts, list):
            lines.append(f"**Next Steps:** {', '.join(nexts[:3])}")
    if persistence.get("files_touched"):
        files = persistence["files_touched"]
        if isinstance(files, list):
            lines.append(f"**Files:** {', '.join(files[:5])}")

    steering = baton.get("steering", {})
    mode = steering.get("mode", "")
    if mode:
        lines.append(f"**Mode:** {mode}")

    gotchas = steering.get("gotchas", [])
    if gotchas and isinstance(gotchas, list):
        lines.append("**Gotchas:**")
        for g in gotchas[:5]:
            lines.append(f"  - {g}")

    constraints = steering.get("constraints", [])
    if constraints and isinstance(constraints, list):
        for c in constraints[:3]:
            lines.append(f"  - CONSTRAINT: {c}")

    decisions = steering.get("decisions_made", [])
    if decisions and isinstance(decisions, list):
        lines.append("**Decisions:**")
        for d in decisions[:3]:
            lines.append(f"  - {d}")

    deps = baton.get("dependency_edges", {})
    if deps and isinstance(deps, dict):
        lines.append("**Dependencies:**")
        for f, info in list(deps.items())[:5]:
            if isinstance(info, dict):
                lines.append(f"  - {f} → {info.get('requires', '?')}")
            else:
                lines.append(f"  - {f} → {info}")

    return "\n".join(lines)


def main():
    """SessionStart hook entry point."""
    try:
        # Read hook input
        input_data = json.load(sys.stdin)
    except Exception:
        input_data = {}

    session_id = input_data.get("session_id", os.environ.get("CLAUDE_SESSION_ID", ""))
    cwd = input_data.get("cwd", os.getcwd())

    # Detect project
    project = _detect_project_name(cwd)
    if not project:
        # Not in a project directory — skip baton
        print(json.dumps({"continue": True}))
        return

    logger.info(f"Baton relay for project: {project}")

    # Call HyperVisa
    baton = _call_baton_api(project, session_id, cwd)

    if not baton or "error" in baton:
        logger.info(f"No baton available: {baton.get('error', 'API unreachable') if baton else 'unreachable'}")
        _write_inject_state(project, False)
        print(json.dumps({"continue": True}))
        return

    # Cache baton for statusline to read
    _cache_baton(baton, project)

    # Format and inject
    context_text = _format_baton_context(baton)

    _write_inject_state(project, True, len(context_text))

    response = {
        "continue": True,
        "additionalContext": context_text,
    }

    logger.info(f"Injected baton: {len(context_text)} chars, purpose={baton.get('purpose', '?')[:50]}")
    print(json.dumps(response))


if __name__ == "__main__":
    main()
