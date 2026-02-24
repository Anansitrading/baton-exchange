#!/usr/bin/env python3
"""Baton Exchange — Claude Code Statusline.

Renders baton telemetry in the Claude Code terminal footer:
  model | baton status + learnings | dir | context% → handoff%

Reads baton state from BATON_STATE_DIR (default: ~/.cortex/baton/).
"""

import json
import os
import sys
import time
from pathlib import Path

# ANSI escape codes
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
ORANGE = "\033[38;5;208m"
RED = "\033[31m"
BOLD_RED = "\033[1;31m"
BLINK_RED = "\033[5;31m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
WHITE = "\033[37m"
DIM_WHITE = "\033[2;37m"

BATON_DIR = Path(os.environ.get("BATON_STATE_DIR", Path.home() / ".cortex" / "baton"))
BATON_REGISTRY = BATON_DIR / "project-contexts.json"
BATON_LAST_INJECT = BATON_DIR / "last-inject.json"
HANDOFF_THRESHOLD = int(os.environ.get("BATON_HANDOFF_PCT", "80"))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _context_segment(remaining_pct: float | None) -> str:
    """Build context % in red + handoff marker."""
    if remaining_pct is None:
        return ""

    rem = round(remaining_pct)
    raw_used = max(0, min(100, 100 - rem))
    used = min(100, round((raw_used / HANDOFF_THRESHOLD) * 100))

    filled = used // 10
    bar = "\u2588" * filled + "\u2591" * (10 - filled)

    if used < 63:
        bar_color = GREEN
    elif used < 81:
        bar_color = YELLOW
    elif used < 95:
        bar_color = ORANGE
    else:
        bar_color = BLINK_RED
        bar = f"\U0001f480 {bar}"

    handoff_str = f" {DIM}\u2192{RESET}{BOLD_RED}{HANDOFF_THRESHOLD}%{RESET}"
    return f" {bar_color}{bar}{RESET} {RED}{used}%{RESET}{handoff_str}"


def _detect_project(cwd: str, projects: dict) -> tuple[str | None, dict | None]:
    """Find current project from git remote or dirname."""
    try:
        import subprocess
        import re

        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2, cwd=cwd,
        )
        if r.returncode == 0:
            m = re.search(r"[/:]([^/:]+?)(?:\.git)?$", r.stdout.strip())
            if m:
                name = m.group(1)
                if name in projects:
                    return name, projects[name]
    except Exception:
        pass

    dirname = Path(cwd).name
    if dirname in projects:
        return dirname, projects[dirname]

    return None, None


def _baton_segment(cwd: str) -> tuple[str, list[str]]:
    """Build baton status segment + learnings list."""
    registry = _read_json(BATON_REGISTRY)
    projects = registry.get("projects", {})

    project_name, project_entry = _detect_project(cwd, projects)

    if not project_entry:
        return f"{DIM_WHITE}\u2300 no baton{RESET}", []

    # Injection age
    last_inject = _read_json(BATON_LAST_INJECT)
    inject_age = ""
    if last_inject.get("timestamp"):
        try:
            age_s = time.time() - last_inject["timestamp"]
            if age_s < 60:
                inject_age = f"{int(age_s)}s"
            elif age_s < 3600:
                inject_age = f"{int(age_s / 60)}m"
            else:
                inject_age = f"{int(age_s / 3600)}h"
        except Exception:
            pass

    # Turn depth
    ctx_id = project_entry.get("context_id", 0)
    head = project_entry.get("head_turn_id", 0)
    depth = head - ctx_id if isinstance(head, int) and isinstance(ctx_id, int) else 0

    # Read baton data — per-project first, then global fallback
    per_project_cache = BATON_DIR / f"baton-{project_name}.json"
    global_cache = BATON_DIR / "last-baton.json"
    baton_data = _read_json(per_project_cache) if per_project_cache.exists() else _read_json(global_cache)

    purpose_short = ""
    mode = ""
    gotchas = []
    learnings = []

    if baton_data.get("project") == project_name or baton_data.get("_meta", {}).get("project") == project_name:
        purpose = baton_data.get("purpose", "")
        if purpose:
            purpose_short = purpose[:40] + ("\u2026" if len(purpose) > 40 else "")

        steering = baton_data.get("steering", {})
        mode = steering.get("mode", "")
        gotchas = steering.get("gotchas", [])
        decisions = steering.get("decisions_made", [])
        constraints = steering.get("constraints", [])

        # Build learnings list
        for g in gotchas[:3]:
            learnings.append(f"{YELLOW}\u26A0{RESET} {g}")
        for d in decisions[:2]:
            learnings.append(f"{GREEN}\u2713{RESET} {d}")
        for c in constraints[:2]:
            learnings.append(f"{RED}\u2297{RESET} {c}")

        persistence = baton_data.get("persistence", {})
        in_progress = persistence.get("in_progress", "")
        if in_progress:
            learnings.insert(0, f"{CYAN}\u25B6{RESET} {in_progress}")
        completed = persistence.get("completed", [])
        if completed:
            count = len(completed)
            last = completed[-1] if completed else ""
            learnings.append(f"{DIM}\u2714 {count} done (last: {last[:30]}){RESET}")

    parts = [f"{CYAN}\u26A1{RESET}"]

    if mode:
        mode_colors = {
            "implement": GREEN, "debug": RED,
            "refactor": YELLOW, "review": MAGENTA,
        }
        mc = mode_colors.get(mode, WHITE)
        parts.append(f"{mc}{mode}{RESET}")

    if depth > 0:
        parts.append(f"{DIM}t{depth}{RESET}")

    if gotchas:
        parts.append(f"{YELLOW}\u26A0{len(gotchas)}{RESET}")

    if purpose_short:
        parts.append(f"{DIM_WHITE}{purpose_short}{RESET}")

    if inject_age:
        parts.append(f"{DIM}@{inject_age}{RESET}")

    return " ".join(parts), learnings


def _current_task(session_id: str) -> str:
    """Get current in-progress task from Claude Code todos."""
    if not session_id:
        return ""

    todos_dir = Path.home() / ".claude" / "todos"
    if not todos_dir.exists():
        return ""

    try:
        files = []
        for f in todos_dir.iterdir():
            if f.name.startswith(session_id) and "-agent-" in f.name and f.suffix == ".json":
                files.append((f, f.stat().st_mtime))
        files.sort(key=lambda x: x[1], reverse=True)

        if files:
            todos = json.loads(files[0][0].read_text())
            for t in todos:
                if t.get("status") == "in_progress":
                    return t.get("activeForm", "")
    except Exception:
        pass
    return ""


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    model = data.get("model", {}).get("display_name", "Claude")
    cwd = data.get("workspace", {}).get("current_dir", os.getcwd())
    session_id = data.get("session_id", "")
    remaining = data.get("context_window", {}).get("remaining_percentage")

    dirname = Path(cwd).name

    baton, learnings = _baton_segment(cwd)
    task = _current_task(session_id)
    ctx = _context_segment(remaining)

    parts = [f"{DIM}{model}{RESET}"]
    parts.append(baton)
    if task:
        parts.append(f"{BOLD}{task}{RESET}")
    parts.append(f"{DIM}{dirname}{RESET}")

    line = " \u2502 ".join(parts)
    line += ctx

    if learnings:
        line += f"\n{DIM}\u2500\u2500 session learnings \u2500\u2500{RESET}"
        for item in learnings[:5]:
            display = item if len(item) < 75 else item[:72] + "\u2026"
            line += f"\n  {display}"

    sys.stdout.write(line)


if __name__ == "__main__":
    main()
