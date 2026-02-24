# Baton Exchange

**Context relay protocol for Claude Code sessions.** Never lose your north star, session state, or hard-won lessons between context windows.

```
Opus 4.6 │ ⚡ refactor t37 ⚠3 Build a self-sustaining context-com… @2m │ my-project █████░░░░░ 55% →80%
── session learnings ──
  ▶ Refactoring auth handler
  ⚠ Auth expires every 25min; use refresh tokens
  ⚠ JSON mode requires response_mime_type param
  ✓ Chose HyperVisa for compression
```

## What It Does

Baton Exchange injects a compressed context payload (~200-300 tokens) at the start of every Claude Code session. The baton carries three pillars:

| Pillar | Purpose | Example |
|--------|---------|---------|
| **Purpose** | North-star objective — never lost | "Build auth system with JWT refresh" |
| **Persistence** | Where you left off | completed tasks, in-progress, next steps, files touched |
| **Steering** | Lessons learned | gotchas, constraints, decisions made |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Claude Code Session                                │
│                                                     │
│  SessionStart hook ──→ HyperVisa /baton API         │
│       │                    │                        │
│       │              Gemini compresses               │
│       │              cxdb timeline + NLM knowledge   │
│       │                    │                        │
│       ◄──── additionalContext (baton JSON) ────┘    │
│                                                     │
│  PreCompact hook ──→ writes turn to cxdb            │
│       │              (project-scoped context)       │
│       ├──→ ingests session to Cortex DuckDB         │
│       └──→ syncs summary to NotebookLM              │
│                                                     │
│  Statusline ──→ reads ~/.cortex/baton/ cache        │
│                 renders in terminal footer           │
└─────────────────────────────────────────────────────┘
```

## Prerequisites

### Required

- **Python 3.10+**
- **Claude Code** CLI (`claude`)
- **[HyperVisa](https://github.com/Anansitrading/hypervisa-cli)** — Gemini-powered baton synthesis engine

  ```bash
  git clone https://github.com/Anansitrading/hypervisa-cli.git
  cd hypervisa-cli && pip install -e .
  export GEMINI_API_KEY="your-key-here"
  hypervisa-api   # runs on port 8042
  ```

- **[cxdb](https://github.com/Anansitrading/cxdb)** — Conversation branching server (turn DAG)

  ```bash
  # cxdb binary at /usr/local/bin/cxdb-server
  # Ports: 9009 (binary protocol), 9010 (HTTP API)
  systemctl enable --now cxdb.service
  ```

### Optional

- **NotebookLM** — Human transparency layer (queries via pipx venv)
- **Cortex DuckDB** — Session forensics database

## Installation

```bash
git clone https://github.com/Anansitrading/baton-exchange.git
cd baton-exchange

# Install Python dependencies
pip install -r requirements.txt

# Install hooks and statusline for Claude Code
./install.sh
```

The installer:
1. Copies the statusline to `~/.claude/hooks/`
2. Symlinks cortex and hypervisa code to `~/.cortex/baton/`
3. Configures `~/.claude/settings.json` with SessionStart, PreCompact hooks, and statusline

## What's In The Box

```
baton-exchange/
├── hooks/
│   └── baton-statusline.py        # Claude Code terminal footer
├── cortex/                        # Vendored from Oracle-Cortex
│   ├── baton.py                   # cxdb project context manager
│   ├── cxdb_client.py             # cxdb binary/HTTP protocol client
│   ├── notebooklm_client.py       # NotebookLM wrapper (subprocess → pipx venv)
│   └── hooks/
│       ├── baton_hook.py          # SessionStart: synthesize + inject baton
│       └── compact_hook.py        # PreCompact: ingest + record to cxdb
├── hypervisa/                     # Vendored from HyperVisa
│   ├── baton.py                   # Gemini synthesis engine (3 pillars)
│   └── gemini.py                  # Gemini API wrapper
├── examples/
│   └── sample-baton.json          # Example baton payload
├── install.sh                     # Global installer
├── uninstall.sh                   # Clean removal
└── requirements.txt               # Python deps (blake3, httpx, msgpack, google-genai)
```

### Upstream Repositories

| Component | Repo | Purpose |
|-----------|------|---------|
| **Oracle-Cortex** | [Anansitrading/Oracle-Cortex](https://github.com/Anansitrading/Oracle-Cortex) | Cognitive architecture — memory, governance, learning |
| **HyperVisa** | [Anansitrading/hypervisa-cli](https://github.com/Anansitrading/hypervisa-cli) | Adaptive video-mediated context engine, Gemini wrapper |
| **cxdb** | [Anansitrading/cxdb](https://github.com/Anansitrading/cxdb) | O(1) conversation branching — turn DAG storage |

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BATON_HYPERVISA_URL` | `http://localhost:8042` | HyperVisa API endpoint |
| `BATON_STATE_DIR` | `~/.cortex/baton` | Baton state directory |
| `BATON_HANDOFF_PCT` | `80` | Context % where auto-compact fires |
| `GEMINI_API_KEY` | — | Required for HyperVisa baton synthesis |

### Services

| Service | Port | Protocol | Required |
|---------|------|----------|----------|
| cxdb | 9009 / 9010 | Binary / HTTP | Yes |
| HyperVisa API | 8042 | HTTP REST | Yes |
| NotebookLM | subprocess | pipx venv | Optional |

## Statusline

The statusline renders in the Claude Code terminal footer:

```
model │ ⚡ mode t{depth} ⚠{gotchas} purpose... @{age} │ dir  ctx% →handoff%
── session learnings ──
  ▶ current in-progress task
  ⚠ gotcha warning
  ✓ decision made
  ⊗ constraint
  ✔ N done (last: ...)
```

| Segment | Meaning |
|---------|---------|
| `⚡` | Baton active for this project |
| `refactor` | Mode badge (green=implement, red=debug, yellow=refactor, magenta=review) |
| `t37` | Turn depth in project timeline (cxdb) |
| `⚠3` | Number of tracked gotchas |
| `Build a self-...` | Purpose summary (truncated to 40 chars) |
| `@2m` | Time since last baton injection |
| `55%` | Context usage (always red) |
| `→80%` | Handoff threshold marker (where auto-compact fires) |

## Baton Schema

```json
{
  "purpose": "One-sentence north star objective",
  "persistence": {
    "last_session": "session-uuid",
    "completed": ["task1", "task2"],
    "in_progress": "current task",
    "next": ["next step 1"],
    "files_touched": ["src/auth.py"]
  },
  "steering": {
    "mode": "implement|debug|refactor|review",
    "gotchas": ["Don't do X because Y"],
    "constraints": ["Use library Z"],
    "decisions_made": ["Chose A over B because C"]
  },
  "dependency_edges": {
    "file.py": {"requires": "other.py", "line": 42}
  }
}
```

## How The Relay Works

1. **Session Start** — `baton_hook.py` fires, detects project from git remote, calls HyperVisa `/baton` API
2. **HyperVisa synthesizes** — queries cxdb for timeline (last 5-10 turns) + NotebookLM for knowledge, feeds to Gemini-3-Flash with compression prompt, returns <400 token JSON baton
3. **Context injected** — baton formatted as Markdown, injected via `additionalContext` field. Cached to disk for statusline
4. **During session** — statusline reads cached baton, shows telemetry in footer
5. **Pre-Compact** — before auto-compaction, session is ingested to DuckDB, summary synced to NotebookLM, and a structured turn appended to the project's cxdb context
6. **Next session** — loop back to step 1 with enriched timeline

## License

MIT
