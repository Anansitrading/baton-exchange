"""Baton Relay Protocol — cxdb project context manager.

Maintains one cxdb context per project directory. Appends structured turns
instead of creating throwaway contexts. Provides read-back for baton synthesis.

Registry: ~/.cortex/baton/project-contexts.json
"""

import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("baton")

REGISTRY_PATH = Path.home() / ".cortex" / "baton" / "project-contexts.json"


def _detect_project_name(cwd: str) -> str:
    """Detect project name from git remote or directory basename."""
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, cwd=cwd,
        )
        if r.returncode == 0:
            url = r.stdout.strip()
            m = re.search(r"[/:]([^/:]+?)(?:\.git)?$", url)
            if m:
                return m.group(1)
    except Exception:
        pass
    basename = Path(cwd).name
    # Skip home directory and system dirs
    if basename in ("devuser", "home", "root", "tmp", ""):
        return f"unnamed-{basename}"
    return basename


def _load_registry() -> dict:
    """Load the project → cxdb context registry."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text())
        except Exception:
            pass
    return {"projects": {}}


def _save_registry(registry: dict) -> None:
    """Save the project → cxdb context registry."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2))


def get_project_context_id(project_name: str) -> int | None:
    """Get the cxdb context ID for a project, or None if not yet created."""
    registry = _load_registry()
    entry = registry.get("projects", {}).get(project_name)
    return entry["context_id"] if entry else None


def ensure_project_context(project_name: str, cwd: str) -> int:
    """Get or create a cxdb context for this project. Returns context_id."""
    from cxdb_client import CxdbClient

    registry = _load_registry()
    projects = registry.setdefault("projects", {})

    if project_name in projects:
        return projects[project_name]["context_id"]

    # Create new context
    client = CxdbClient(client_tag="baton-relay")
    ctx = client.create_context()
    client.close()

    projects[project_name] = {
        "context_id": ctx.context_id,
        "head_turn_id": ctx.head_turn_id,
        "cwd": cwd,
        "created": datetime.now().isoformat(),
    }
    _save_registry(registry)

    logger.info(f"Created cxdb context {ctx.context_id} for project {project_name}")
    return ctx.context_id


def append_session_turn(
    project_name: str,
    cwd: str,
    session_id: str,
    summary: str,
    event_type: str = "compact",
    gotchas: list[str] | None = None,
    decisions: list[str] | None = None,
    progress: dict | None = None,
) -> int:
    """Append a structured turn to the project's cxdb context.

    Returns the new turn_id.
    """
    from cxdb_client import CxdbClient

    context_id = ensure_project_context(project_name, cwd)

    metadata = {
        "event": event_type,
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(),
        "cwd": cwd,
        "project": project_name,
    }
    if gotchas:
        metadata["gotchas"] = json.dumps(gotchas)
    if decisions:
        metadata["decisions"] = json.dumps(decisions)
    if progress:
        metadata["progress"] = json.dumps(progress)

    client = CxdbClient(client_tag="baton-relay")
    turn = client.append_turn(
        context_id=context_id,
        role="system",
        content=summary,
        metadata=metadata,
    )
    client.close()

    # Update registry head
    registry = _load_registry()
    if project_name in registry.get("projects", {}):
        registry["projects"][project_name]["head_turn_id"] = turn.turn_id
        registry["projects"][project_name]["last_session"] = session_id
        registry["projects"][project_name]["updated"] = datetime.now().isoformat()
        _save_registry(registry)

    logger.info(
        f"Appended turn {turn.turn_id} to project {project_name} "
        f"(ctx={context_id}, event={event_type})"
    )
    return turn.turn_id


def get_recent_turns(project_name: str, limit: int = 10) -> list[dict]:
    """Read back recent turns from a project's cxdb context.

    Returns list of dicts with role, content, metadata, turn_id.
    """
    from cxdb_client import CxdbClient

    context_id = get_project_context_id(project_name)
    if context_id is None:
        return []

    client = CxdbClient(client_tag="baton-relay-read")
    try:
        turns = client.get_last(context_id, limit=limit, include_payload=True)
    except Exception as e:
        logger.warning(f"Failed to read cxdb turns for {project_name}: {e}")
        return []
    finally:
        client.close()

    result = []
    for t in turns:
        data = t.data
        if not data:
            continue
        entry = {
            "turn_id": t.turn_id,
            "role": data.get(1, "unknown"),
            "content": data.get(2, ""),
            "timestamp": data.get(3, 0),
            "metadata": data.get(4, {}),
        }
        result.append(entry)

    return result


def get_all_projects() -> dict:
    """Return the full project registry."""
    return _load_registry().get("projects", {})
