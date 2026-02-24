"""NotebookLM client wrapper using subprocess to call pipx-installed server.

Loads auth from ~/.notebooklm-mcp/auth.json (cookies dict, csrf_token, session_id)
and calls NotebookLMClient in the pipx venv with correct constructor args.
"""

import json
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass


PIPX_VENV_PYTHON = Path.home() / ".local/share/pipx/venvs/notebooklm-mcp-server/bin/python"
AUTH_PATH = Path.home() / ".notebooklm-mcp" / "auth.json"

# Preamble injected into every subprocess — loads auth and creates client
_CLIENT_PREAMBLE = '''
import json
from pathlib import Path
from notebooklm_mcp.api_client import NotebookLMClient

_auth_path = Path.home() / ".notebooklm-mcp" / "auth.json"
_auth = json.loads(_auth_path.read_text())
client = NotebookLMClient(
    cookies=_auth["cookies"],
    csrf_token=_auth.get("csrf_token", ""),
    session_id=_auth.get("session_id", ""),
)
'''


@dataclass
class NotebookLMResult:
    """Result from NotebookLM operation."""
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def is_available() -> bool:
    """Check if notebooklm-mcp-server is installed and auth exists."""
    return PIPX_VENV_PYTHON.exists() and AUTH_PATH.exists()


def _run_in_pipx_venv(code: str, timeout: int = 60) -> NotebookLMResult:
    """Run Python code in the pipx venv containing notebooklm-mcp-server."""
    if not PIPX_VENV_PYTHON.exists():
        return NotebookLMResult(success=False, error="pipx venv not found")

    if not AUTH_PATH.exists():
        return NotebookLMResult(success=False, error="auth.json not found")

    full_code = _CLIENT_PREAMBLE + code

    try:
        result = subprocess.run(
            [str(PIPX_VENV_PYTHON), "-c", full_code],
            capture_output=True, text=True, timeout=timeout,
        )

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if any(k in stderr for k in ("auth", "cookie", "session", "403", "401")):
                return NotebookLMResult(
                    success=False,
                    error=f"Auth expired: {result.stderr.strip()[:200]}"
                )
            return NotebookLMResult(
                success=False,
                error=result.stderr.strip()[:300] or "Unknown error"
            )

        try:
            data = json.loads(result.stdout)
            return NotebookLMResult(success=True, data=data)
        except json.JSONDecodeError:
            return NotebookLMResult(success=True, data={"output": result.stdout.strip()})

    except subprocess.TimeoutExpired:
        return NotebookLMResult(success=False, error="Operation timed out")
    except Exception as e:
        return NotebookLMResult(success=False, error=str(e))


def list_notebooks() -> NotebookLMResult:
    """List all notebooks."""
    return _run_in_pipx_venv('''
result = [{"id": n.id, "title": n.title} for n in client.list_notebooks()]
print(json.dumps(result))
''')


def add_text_source(notebook_id: str, text: str, title: str = "Pasted Text") -> NotebookLMResult:
    """Add text content as a source to a notebook."""
    # Pass data via env to avoid escaping nightmares
    import os
    env = os.environ.copy()
    env['_NB_ID'] = notebook_id
    env['_NB_TEXT'] = text
    env['_NB_TITLE'] = title

    code = _CLIENT_PREAMBLE + '''
import os
result = client.add_text_source(
    notebook_id=os.environ["_NB_ID"],
    text=os.environ["_NB_TEXT"],
    title=os.environ["_NB_TITLE"],
)
if result:
    print(json.dumps({"status": "success", "source_id": result.get("id", "")}))
else:
    print(json.dumps({"status": "error", "error": "Failed to add source"}))
'''

    if not PIPX_VENV_PYTHON.exists():
        return NotebookLMResult(success=False, error="pipx venv not found")
    if not AUTH_PATH.exists():
        return NotebookLMResult(success=False, error="auth.json not found")

    try:
        result = subprocess.run(
            [str(PIPX_VENV_PYTHON), "-c", code],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if result.returncode != 0:
            return NotebookLMResult(success=False, error=result.stderr.strip()[:300])
        try:
            data = json.loads(result.stdout)
            return NotebookLMResult(success=True, data=data)
        except json.JSONDecodeError:
            return NotebookLMResult(success=True, data={"output": result.stdout.strip()})
    except subprocess.TimeoutExpired:
        return NotebookLMResult(success=False, error="Operation timed out")
    except Exception as e:
        return NotebookLMResult(success=False, error=str(e))


def get_notebook(notebook_id: str) -> NotebookLMResult:
    """Get notebook details."""
    return _run_in_pipx_venv(f'''
result = client.get_notebook("{notebook_id}")
if result:
    print(json.dumps(result))
else:
    print(json.dumps({{"error": "Notebook not found"}}))
''')


def query_notebook(notebook_id: str, query: str, timeout: int = 120) -> NotebookLMResult:
    """Query a notebook."""
    import os
    env = os.environ.copy()
    env['_NB_ID'] = notebook_id
    env['_NB_QUERY'] = query

    code = _CLIENT_PREAMBLE + '''
import os
result = client.query(
    notebook_id=os.environ["_NB_ID"],
    query=os.environ["_NB_QUERY"],
)
if result:
    print(json.dumps({"status": "success", "answer": result.get("answer", ""), "sources": result.get("sources", [])}))
else:
    print(json.dumps({"status": "error", "error": "Query failed"}))
'''

    if not PIPX_VENV_PYTHON.exists():
        return NotebookLMResult(success=False, error="pipx venv not found")
    if not AUTH_PATH.exists():
        return NotebookLMResult(success=False, error="auth.json not found")

    try:
        result = subprocess.run(
            [str(PIPX_VENV_PYTHON), "-c", code],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode != 0:
            return NotebookLMResult(success=False, error=result.stderr.strip()[:300])
        try:
            data = json.loads(result.stdout)
            return NotebookLMResult(success=True, data=data)
        except json.JSONDecodeError:
            return NotebookLMResult(success=True, data={"output": result.stdout.strip()})
    except subprocess.TimeoutExpired:
        return NotebookLMResult(success=False, error="Operation timed out")
    except Exception as e:
        return NotebookLMResult(success=False, error=str(e))


def get_weekly_em_notebook_id() -> Optional[str]:
    """Get the current week's Oracle-EM notebook ID from weekly state."""
    state_path = Path.home() / ".cortex" / "state" / "weekly_notebooks.json"
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text())
        current_week = state.get("current_week", "")
        notebooks = state.get("notebooks", {})
        entry = notebooks.get(current_week, {})
        return entry.get("notebook_id")
    except Exception:
        return None


def get_project_hivemind_id(cwd: str) -> Optional[str]:
    """Get the project hivemind notebook ID from the registry."""
    registry_path = Path.home() / ".cortex" / "smith" / "hivemind-registry.json"
    if not registry_path.exists():
        return None
    try:
        import re, subprocess as sp
        registry = json.loads(registry_path.read_text())
        projects = registry.get("projects", {})

        # Try git remote name first
        try:
            r = sp.run(['git', 'remote', 'get-url', 'origin'],
                       capture_output=True, text=True, timeout=5, cwd=cwd)
            if r.returncode == 0:
                url = r.stdout.strip()
                m = re.search(r'[/:]([^/:]+?)(?:\.git)?$', url)
                if m:
                    name = m.group(1)
                    if name in projects:
                        return projects[name]['notebook_id']
        except Exception:
            pass

        # Try basename
        basename = Path(cwd).name
        if basename in projects:
            return projects[basename]['notebook_id']

        return None
    except Exception:
        return None
