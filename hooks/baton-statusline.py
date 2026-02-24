#!/usr/bin/env python3
"""Baton Relay Statusline for Claude Code.

Shows: model | baton status | dir
       Claude ctx bar  HyperVisa ctx bar
       session learnings

Reads baton state from ~/.cortex/baton/.
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
BLUE = "\033[34m"
BOLD_CYAN = "\033[1;36m"

BATON_DIR = Path.home() / ".cortex" / "baton"
BATON_REGISTRY = BATON_DIR / "project-contexts.json"
BATON_LAST_INJECT = BATON_DIR / "last-inject.json"
HYPERVISA_STATS = BATON_DIR / "hypervisa-stats.json"

# Handoff fires at 80% used (20% remaining). Show marker at that point.
HANDOFF_THRESHOLD = 80


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _make_bar(used_pct: int, width: int = 10) -> str:
    """Build a block bar: ████░░░░░░"""
    filled = max(0, min(width, used_pct * width // 100))
    return "\u2588" * filled + "\u2591" * (width - filled)


def _claude_context_line(remaining_pct: float | None) -> str:
    """Build Claude context bar with real %, handoff marker at 80%.

    No scaling — bar fills proportionally to actual context usage.
    At 80% used (20% remaining), auto-compact fires and baton exchange happens.
    """
    if remaining_pct is None:
        return ""

    rem = round(remaining_pct)
    used = max(0, min(100, 100 - rem))

    # 10-block bar — 8 blocks = 80% = handoff threshold
    filled = max(0, min(10, used * 10 // 100))
    # Build bar with handoff marker at position 8 (80%)
    blocks = []
    for i in range(10):
        if i < filled:
            blocks.append("\u2588")
        elif i == 8:
            # Handoff position marker (80%) — show as distinct char when unfilled
            blocks.append("\u2595")
        else:
            blocks.append("\u2591")
    bar = "".join(blocks)

    if used < 50:
        bar_color = GREEN
    elif used < 70:
        bar_color = YELLOW
    elif used < 80:
        bar_color = ORANGE
    else:
        bar_color = BLINK_RED
        bar = f"\U0001f480 {bar}"

    # Show handoff label
    if used < 80:
        handoff = f" {DIM}baton\u2192{RESET}{BOLD_RED}80%{RESET}"
    else:
        handoff = f" {BOLD_RED}\u26A1 BATON EXCHANGE{RESET}"

    return f"{DIM}Claude{RESET} {bar_color}{bar}{RESET} {RED}{used}%{RESET}{handoff}"


def _hypervisa_context_line() -> str:
    """Build HyperVisa context bar: HV ████░░░░░░ 42% 421K/1M"""
    stats = _read_json(HYPERVISA_STATS)
    if not stats:
        return ""

    total = stats.get("total_tokens", 0)
    limit = stats.get("context_limit", 1_000_000)
    active = stats.get("active_sessions", 0)

    if limit <= 0:
        return ""

    used_pct = min(100, round(total / limit * 100))
    bar = _make_bar(used_pct)

    # Color based on utilization
    if used_pct < 50:
        bar_color = CYAN
    elif used_pct < 75:
        bar_color = BLUE
    elif used_pct < 90:
        bar_color = YELLOW
    else:
        bar_color = ORANGE

    # Format token count: 421K, 1.2M, etc.
    if total >= 1_000_000:
        tok_str = f"{total / 1_000_000:.1f}M"
    elif total >= 1_000:
        tok_str = f"{total // 1_000}K"
    else:
        tok_str = str(total)

    limit_str = f"{limit // 1_000_000}M" if limit >= 1_000_000 else f"{limit // 1_000}K"

    return (
        f"{BOLD_CYAN}HV{RESET} {bar_color}{bar}{RESET} "
        f"{CYAN}{used_pct}%{RESET} {DIM}{tok_str}/{limit_str} "
        f"({active} sessions){RESET}"
    )


def _detect_project(cwd: str, projects: dict) -> tuple[str | None, dict | None]:
    """Find current project from git remote or dirname."""
    # Try git remote
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

    # Fallback to dirname
    dirname = Path(cwd).name
    if dirname in projects:
        return dirname, projects[dirname]

    return None, None


def _baton_segment(cwd: str) -> tuple[str, list[str]]:
    """Build baton status segment + learnings list. Returns (segment, learnings)."""
    registry = _read_json(BATON_REGISTRY)
    projects = registry.get("projects", {})

    project_name, project_entry = _detect_project(cwd, projects)

    # Fallback: if cwd doesn't match a known project, use the last injected project.
    if not project_entry:
        last_inject = _read_json(BATON_LAST_INJECT)
        fallback_project = last_inject.get("project", "")
        if fallback_project and fallback_project in projects:
            project_name = fallback_project
            project_entry = projects[fallback_project]
        else:
            global_baton = _read_json(BATON_DIR / "last-baton.json")
            gp = global_baton.get("project", "")
            if gp and gp in projects:
                project_name = gp
                project_entry = projects[gp]

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

        # Build learnings list from gotchas + decisions + constraints
        for g in gotchas[:3]:
            learnings.append(f"{YELLOW}\u26A0{RESET} {g}")
        for d in decisions[:2]:
            learnings.append(f"{GREEN}\u2713{RESET} {d}")
        for c in constraints[:2]:
            learnings.append(f"{RED}\u2297{RESET} {c}")

        # Add persistence info
        persistence = baton_data.get("persistence", {})
        in_progress = persistence.get("in_progress", "")
        if in_progress:
            learnings.insert(0, f"{CYAN}\u25B6{RESET} {in_progress}")
        completed = persistence.get("completed", [])
        if completed:
            count = len(completed)
            last = completed[-1] if completed else ""
            learnings.append(f"{DIM}\u2714 {count} done (last: {last[:30]}){RESET}")

    parts = []
    parts.append(f"{CYAN}\u26A1{RESET}")

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
    """Get current in-progress task from todos."""
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

    # Build segments
    baton, learnings = _baton_segment(cwd)
    task = _current_task(session_id)

    # Line 1: model | baton | [task |] dir
    header_parts = [f"{DIM}{model}{RESET}"]
    header_parts.append(baton)
    if task:
        header_parts.append(f"{BOLD}{task}{RESET}")
    header_parts.append(f"{DIM}{dirname}{RESET}")

    line = " \u2502 ".join(header_parts)

    # Line 2: dual context bars — Claude + HyperVisa side by side
    claude_bar = _claude_context_line(remaining)
    hv_bar = _hypervisa_context_line()

    if claude_bar or hv_bar:
        ctx_parts = []
        if claude_bar:
            ctx_parts.append(claude_bar)
        if hv_bar:
            ctx_parts.append(hv_bar)
        line += "\n  " + "  \u2502  ".join(ctx_parts)

    # Append learnings as compact summary
    if learnings:
        line += f"\n{DIM}\u2500\u2500 session learnings \u2500\u2500{RESET}"
        for item in learnings[:5]:
            display = item if len(item) < 75 else item[:72] + "\u2026"
            line += f"\n  {display}"

    sys.stdout.write(line)


if __name__ == "__main__":
    main()
