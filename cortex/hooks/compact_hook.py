#!/usr/bin/env python3
"""
Cortex Compact Hook - Triggered on Claude Code auto/manual compaction.

This hook ensures session persistence by:
1. Ingesting the current session into Cortex DuckDB
2. Syncing a summary to NotebookLM for human transparency

Usage: Called by Claude Code PreCompact hook with JSON input on stdin:
{
  "session_id": "abc123",
  "transcript_path": "~/.claude/projects/.../session.jsonl",
  "hook_event_name": "PreCompact",
  "trigger": "auto" | "manual",
  "custom_instructions": ""
}

Output: JSON with optional continue/reason fields
"""

import json
import sys
import os
import logging
from datetime import datetime
from pathlib import Path

# Setup logging to stderr (stdout is for hook response)
logging.basicConfig(
    level=logging.INFO,
    format='[cortex-hook] %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# Add scripts directory to path for imports
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def ingest_session(session_id: str, transcript_path: str) -> dict:
    """Ingest the session into Cortex DuckDB."""
    try:
        from config import load_config
        from db import CortexDB
        from parser import parse_jsonl_line, extract_tool_calls, extract_thinking_blocks

        config = load_config()
        db = CortexDB(config)

        transcript = Path(transcript_path).expanduser()
        if not transcript.exists():
            logger.warning(f"Transcript not found: {transcript}")
            return {"status": "skipped", "reason": "transcript_not_found"}

        # Parse and ingest events
        events = []
        with open(transcript, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    event = parse_jsonl_line(line, session_id)
                    events.append(event)

        if not events:
            logger.info("No events to ingest")
            return {"status": "skipped", "reason": "no_events"}

        # Upsert session
        first_event = events[0]
        db.upsert_session(
            session_id,
            project_path=str(transcript.parent),
            cwd=first_event.cwd,
            git_branch=first_event.git_branch
        )

        # Batch insert events (handles duplicates gracefully)
        inserted, skipped = db.insert_events_batch(events)

        # Process tool calls and thinking blocks for new events
        for event, event_id in inserted:
            for tc in extract_tool_calls(event):
                db.insert_tool_call(event_id, session_id, tc, event.timestamp)
            for tb in extract_thinking_blocks(event):
                db.insert_thinking_block(event_id, session_id, tb, event.timestamp)

        # Update session stats
        db.update_session_stats(session_id)
        db.close()

        logger.info(f"Ingested session {session_id[:12]}...: {len(inserted)} new events, {skipped} duplicates")
        return {
            "status": "success",
            "events_inserted": len(inserted),
            "events_skipped": skipped,
            "total_events": len(events)
        }

    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        return {"status": "error", "error": str(e)}


def generate_session_summary(session_id: str) -> str:
    """Generate a concise summary of the session for NotebookLM."""
    try:
        from config import load_config
        from db import CortexDB

        config = load_config()
        db = CortexDB(config)

        # Get session info
        sessions = db.get_sessions(limit=1)
        session = next((s for s in sessions if s['session_id'] == session_id), None)

        if not session:
            return f"Session {session_id[:12]} compacted - no details available"

        # Get stats
        stats = db.execute_query("""
            SELECT
                COUNT(*) as events,
                SUM(CASE WHEN type = 'user' THEN 1 ELSE 0 END) as user_msgs,
                SUM(CASE WHEN type = 'assistant' THEN 1 ELSE 0 END) as assistant_msgs,
                SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) as tokens
            FROM events WHERE session_id = ?
        """, [session_id])

        # Get top tools
        tools = db.execute_query("""
            SELECT tool_name, COUNT(*) as uses
            FROM tool_calls WHERE session_id = ?
            GROUP BY tool_name ORDER BY uses DESC LIMIT 5
        """, [session_id])

        # Get error count
        errors = db.execute_query("""
            SELECT COUNT(*) as count FROM errors WHERE session_id = ?
        """, [session_id])

        db.close()

        stat = stats[0] if stats else {}
        error_count = errors[0]['count'] if errors else 0

        # Build summary
        project = session.get('project_path', '').split('/')[-1] if session.get('project_path') else 'Unknown'
        branch = session.get('git_branch', 'unknown')

        summary = f"""## Session Compacted: {session_id[:12]}
**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M')}
**Project:** {project}
**Branch:** {branch}
**Events:** {stat.get('events', 0)} ({stat.get('user_msgs', 0)} user, {stat.get('assistant_msgs', 0)} assistant)
**Tokens:** {stat.get('tokens', 0):,}
**Errors:** {error_count}

### Tools Used
"""
        if tools:
            for t in tools:
                summary += f"- {t['tool_name']}: {t['uses']} calls\n"
        else:
            summary += "- No tool calls recorded\n"

        return summary

    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        return f"Session {session_id[:12]} compacted at {datetime.now().isoformat()}"


def sync_to_notebooklm(session_id: str, summary: str) -> dict:
    """Sync session summary to NotebookLM — weekly EM + project hivemind."""
    try:
        from notebooklm_client import is_available, add_text_source
        from notebooklm_client import get_weekly_em_notebook_id, get_project_hivemind_id

        if not is_available():
            logger.info("NotebookLM client not available - skipping sync")
            return {"status": "skipped", "reason": "client_not_available"}

        results = {}
        title = f"Compact: {session_id[:12]} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        # 1. Sync to weekly Oracle-EM notebook
        em_id = get_weekly_em_notebook_id()
        if em_id:
            r = add_text_source(em_id, summary, title)
            results["weekly_em"] = "success" if r.success else r.error
            logger.info(f"Weekly EM sync: {'OK' if r.success else r.error}")
        else:
            results["weekly_em"] = "no_weekly_notebook"

        # 2. Sync to project hivemind notebook
        hive_id = get_project_hivemind_id(os.getcwd())
        if hive_id and hive_id != em_id:
            r = add_text_source(hive_id, summary, title)
            results["hivemind"] = "success" if r.success else r.error
            logger.info(f"Hivemind sync: {'OK' if r.success else r.error}")

        return {"status": "success", "details": results}

    except Exception as e:
        logger.warning(f"NotebookLM sync failed: {e}")
        return {"status": "error", "error": str(e)}


def record_to_cxdb(session_id: str, summary: str) -> dict:
    """Record compaction event to cxdb — project-scoped context (Baton Relay)."""
    try:
        from cxdb_client import CxdbClient
        from baton import append_session_turn, _detect_project_name

        client = CxdbClient(client_tag='cortex-compact-hook')
        if not client.health():
            logger.info("cxdb not reachable - skipping")
            client.close()
            return {"status": "skipped", "reason": "cxdb_unreachable"}
        client.close()

        cwd = os.getcwd()
        project_name = _detect_project_name(cwd)

        turn_id = append_session_turn(
            project_name=project_name,
            cwd=cwd,
            session_id=session_id,
            summary=summary,
            event_type="compact",
        )

        logger.info(f"Baton relay: appended turn {turn_id} to project {project_name}")
        return {"status": "success", "project": project_name, "turn_id": turn_id}

    except Exception as e:
        logger.warning(f"cxdb recording failed: {e}")
        return {"status": "error", "error": str(e)}


def main():
    """Main hook handler."""
    try:
        # Read hook input from stdin
        input_data = json.load(sys.stdin)

        session_id = input_data.get('session_id', '')
        transcript_path = input_data.get('transcript_path', '')
        trigger = input_data.get('trigger', 'unknown')
        hook_event = input_data.get('hook_event_name', 'PreCompact')

        logger.info(f"Compact hook triggered: {hook_event} ({trigger})")
        logger.info(f"Session: {session_id[:12]}...")

        results = {
            "hook": "cortex-compact",
            "session_id": session_id,
            "trigger": trigger,
            "timestamp": datetime.now().isoformat(),
        }

        # 1. Ingest session into Cortex DuckDB
        if transcript_path:
            ingest_result = ingest_session(session_id, transcript_path)
            results["ingestion"] = ingest_result

        # 2. Generate summary
        summary = generate_session_summary(session_id)

        # 3. Sync to NotebookLM (human transparency layer)
        notebooklm_result = sync_to_notebooklm(session_id, summary)
        results["notebooklm"] = notebooklm_result

        # 4. Record to cxdb Turn DAG
        cxdb_result = record_to_cxdb(session_id, summary)
        results["cxdb"] = cxdb_result

        # Output success response (Claude Code expects JSON)
        response = {
            "continue": True,  # Allow compaction to proceed
            "suppressOutput": True,  # Don't clutter transcript
        }
        print(json.dumps(response))

        # Log summary to stderr for debugging
        logger.info(f"Hook complete: ingestion={results.get('ingestion', {}).get('status')}, "
                   f"notebooklm={results.get('notebooklm', {}).get('status')}, "
                   f"cxdb={results.get('cxdb', {}).get('status')}")

    except json.JSONDecodeError:
        # No input or invalid JSON - still allow compaction
        logger.warning("No valid JSON input received")
        print(json.dumps({"continue": True}))

    except Exception as e:
        logger.error(f"Hook failed: {e}")
        # Don't block compaction on hook failure
        print(json.dumps({"continue": True}))
        sys.exit(0)  # Exit cleanly even on error


if __name__ == "__main__":
    main()
