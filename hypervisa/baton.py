"""Baton Relay Protocol — Synthesis engine.

Queries cxdb for project timeline + NotebookLM for knowledge,
then uses Gemini to compress into a <500 token baton JSON.

The baton has three pillars:
  - Purpose: north-star objective (never lost)
  - Persistence: seamless continuation from last session
  - Steering: gotchas and lessons learned
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("hypervisa.baton")

# Add cortex scripts to path for cxdb/notebooklm access.
# When installed via baton-exchange, cortex/ is a sibling directory.
# When running inside Oracle-Cortex, it's at scripts/cortex/.
_HERE = Path(__file__).resolve().parent
_CORTEX_CANDIDATES = [
    _HERE.parent / "cortex",                                    # baton-exchange repo layout
    Path("/home/devuser/Oracle-Cortex/scripts/cortex"),         # direct Oracle-Cortex install
    Path.home() / ".cortex" / "baton" / "lib" / "cortex",      # global install
]
for _candidate in _CORTEX_CANDIDATES:
    if _candidate.exists() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
        break

BATON_SYSTEM_PROMPT = """\
You are a context-compression engine for an AI coding agent (Claude Code).
Your job: synthesize raw session history and knowledge into a BATON — a compressed
JSON payload that gives the agent maximum behavioral activation per token.

The baton has exactly three pillars:
1. PURPOSE — The north-star objective. What is the agent working toward? One sentence.
2. PERSISTENCE — Where did the last session leave off? What's done, what's next?
3. STEERING — Gotchas, constraints, lessons learned. What NOT to do.

Rules:
- Output ONLY valid JSON. No markdown, no commentary.
- Keep total output under 400 tokens.
- Use terse, directive language. No prose.
- dependency_edges: only include files actively being modified.
- gotchas: only include things that actually went wrong or are non-obvious.
- If the session history is empty, return a minimal baton with just the project name.

Output schema:
{
  "purpose": "One-sentence north star objective",
  "persistence": {
    "last_session": "session_id",
    "completed": ["task1", "task2"],
    "in_progress": "current task description",
    "next": ["next step 1", "next step 2"],
    "files_touched": ["path/to/file.py:42"]
  },
  "steering": {
    "mode": "implement|debug|refactor|review",
    "gotchas": ["Don't do X because Y"],
    "constraints": ["Use library Z", "Max 300 lines per file"],
    "decisions_made": ["Chose approach A over B because C"]
  },
  "dependency_edges": {
    "file.py": {"requires": "other.py", "line": 42}
  }
}"""


def _get_cxdb_timeline(project_name: str, limit: int = 10) -> list[dict]:
    """Get recent turns from cxdb for this project."""
    try:
        from baton import get_recent_turns
        return get_recent_turns(project_name, limit=limit)
    except Exception as e:
        logger.warning(f"cxdb read failed for {project_name}: {e}")
        return []


def _get_notebooklm_context(
    project_name: str, query: str
) -> str | None:
    """Query NotebookLM hivemind for project-specific knowledge."""
    try:
        from notebooklm_client import is_available, query_notebook
        from baton import _detect_project_name

        if not is_available():
            return None

        # Look up hivemind notebook for this project
        registry_path = Path.home() / ".cortex" / "smith" / "hivemind-registry.json"
        if not registry_path.exists():
            return None

        registry = json.loads(registry_path.read_text())
        projects = registry.get("projects", {})
        entry = projects.get(project_name)
        if not entry:
            return None

        notebook_id = entry["notebook_id"]
        result = query_notebook(notebook_id, query, timeout=30)
        if result.success and result.data:
            return result.data.get("answer", "")
        return None

    except Exception as e:
        logger.warning(f"NotebookLM query failed for {project_name}: {e}")
        return None


def synthesize_baton(
    project_name: str,
    session_id: str | None = None,
    cwd: str | None = None,
    compression: str = "normal",
) -> dict[str, Any]:
    """Synthesize a baton for the given project.

    Args:
        project_name: Project identifier (e.g., "Oracle-Cortex")
        session_id: Current session ID (optional, for context)
        cwd: Current working directory
        compression: "normal" or "ultra" (for near-limit contexts)

    Returns:
        Baton dict with purpose, persistence, steering, dependency_edges.
    """
    from . import gemini as gm

    # 1. Get cxdb timeline
    limit = 5 if compression == "ultra" else 10
    timeline = _get_cxdb_timeline(project_name, limit=limit)

    # 2. Get NotebookLM context (what went wrong before, key patterns)
    nlm_context = _get_notebooklm_context(
        project_name,
        f"What are the key gotchas, recent issues, and current status for {project_name}?",
    )

    # 3. Build raw context for Gemini
    raw_parts = [f"Project: {project_name}"]
    if cwd:
        raw_parts.append(f"Working directory: {cwd}")
    if session_id:
        raw_parts.append(f"Current session: {session_id}")

    if timeline:
        raw_parts.append("\n--- SESSION HISTORY (most recent first) ---")
        for t in timeline:
            meta = t.get("metadata", {})
            raw_parts.append(
                f"[{meta.get('event', '?')}] session={meta.get('session_id', '?')[:12]} "
                f"| {t['content'][:500]}"
            )
            # Include gotchas/decisions from metadata
            if meta.get("gotchas"):
                try:
                    gotchas = json.loads(meta["gotchas"])
                    raw_parts.append(f"  GOTCHAS: {gotchas}")
                except Exception:
                    pass
            if meta.get("decisions"):
                try:
                    decisions = json.loads(meta["decisions"])
                    raw_parts.append(f"  DECISIONS: {decisions}")
                except Exception:
                    pass
    else:
        raw_parts.append("\n(No session history available — this is a fresh project)")

    if nlm_context:
        raw_parts.append(f"\n--- NOTEBOOKLM KNOWLEDGE ---\n{nlm_context[:1000]}")

    raw_text = "\n".join(raw_parts)

    # 4. Compress via Gemini
    try:
        client = gm.make_client()
        model = gm.DEFAULT_MODEL

        from google.genai import types as gtypes

        response = client.models.generate_content(
            model=model,
            contents=raw_text,
            config=gtypes.GenerateContentConfig(
                system_instruction=BATON_SYSTEM_PROMPT,
                temperature=0.2,
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )

        baton_text = response.text.strip()

        # Strip markdown code fences if present
        if baton_text.startswith("```"):
            lines = baton_text.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            baton_text = "\n".join(lines)

        baton = json.loads(baton_text)
        baton["_meta"] = {
            "project": project_name,
            "generated_at": __import__("datetime").datetime.now().isoformat(),
            "compression": compression,
            "cxdb_turns": len(timeline),
            "nlm_available": nlm_context is not None,
        }
        return baton

    except json.JSONDecodeError as e:
        logger.error(f"Gemini returned invalid JSON: {e}")
        # Return a minimal fallback baton
        return _fallback_baton(project_name, timeline, session_id)

    except Exception as e:
        logger.error(f"Baton synthesis failed: {e}")
        return _fallback_baton(project_name, timeline, session_id)


def _fallback_baton(
    project_name: str,
    timeline: list[dict],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Generate a minimal baton without Gemini (fallback)."""
    baton: dict[str, Any] = {
        "purpose": f"Continue work on {project_name}",
        "persistence": {
            "last_session": session_id or "unknown",
            "completed": [],
            "in_progress": "",
            "next": [],
            "files_touched": [],
        },
        "steering": {
            "mode": "implement",
            "gotchas": [],
            "constraints": [],
            "decisions_made": [],
        },
        "dependency_edges": {},
        "_meta": {
            "project": project_name,
            "generated_at": __import__("datetime").datetime.now().isoformat(),
            "compression": "fallback",
            "cxdb_turns": len(timeline),
            "nlm_available": False,
        },
    }

    # Extract what we can from raw timeline
    if timeline:
        latest = timeline[-1]  # Most recent
        meta = latest.get("metadata", {})
        baton["persistence"]["last_session"] = meta.get("session_id", "unknown")
        baton["persistence"]["in_progress"] = latest["content"][:200]

        # Collect gotchas from all turns
        all_gotchas = []
        for t in timeline:
            m = t.get("metadata", {})
            if m.get("gotchas"):
                try:
                    all_gotchas.extend(json.loads(m["gotchas"]))
                except Exception:
                    pass
        baton["steering"]["gotchas"] = all_gotchas[:5]

    return baton
