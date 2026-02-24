#!/usr/bin/env bash
set -euo pipefail

# Baton Exchange — Installer for Claude Code
# Installs hooks, statusline, cortex scripts, and HyperVisa baton engine.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
BATON_DIR="$HOME/.cortex/baton"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"

echo "=== Baton Exchange Installer ==="
echo ""

# 1. Create directories
echo "[1/6] Creating directories..."
mkdir -p "$HOOKS_DIR" "$BATON_DIR"

# 2. Copy statusline (standalone, no deps)
echo "[2/6] Installing statusline..."
cp "$SCRIPT_DIR/hooks/baton-statusline.py" "$HOOKS_DIR/"
chmod +x "$HOOKS_DIR/baton-statusline.py"

# 3. Symlink cortex and hypervisa code
echo "[3/6] Linking cortex and hypervisa code..."

# Remove stale symlinks/dirs if they exist
rm -f "$BATON_DIR/cortex" "$BATON_DIR/hypervisa" 2>/dev/null || true
rm -rf "$BATON_DIR/cortex" "$BATON_DIR/hypervisa" 2>/dev/null || true

ln -sf "$SCRIPT_DIR/cortex" "$BATON_DIR/cortex"
ln -sf "$SCRIPT_DIR/hypervisa" "$BATON_DIR/hypervisa"
echo "  cortex → $SCRIPT_DIR/cortex"
echo "  hypervisa → $SCRIPT_DIR/hypervisa"

# 4. Install Python dependencies
echo "[4/6] Checking Python dependencies..."
MISSING=()
python3 -c "import blake3" 2>/dev/null || MISSING+=("blake3")
python3 -c "import httpx" 2>/dev/null || MISSING+=("httpx")
python3 -c "import msgpack" 2>/dev/null || MISSING+=("msgpack")
python3 -c "import google.genai" 2>/dev/null || MISSING+=("google-genai")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  Installing missing packages: ${MISSING[*]}"
    pip install "${MISSING[@]}" --quiet 2>/dev/null || \
    pip3 install "${MISSING[@]}" --quiet 2>/dev/null || \
    echo "  WARNING: Could not install ${MISSING[*]}. Run: pip install -r $SCRIPT_DIR/requirements.txt"
else
    echo "  All dependencies satisfied"
fi

# 5. Update Claude Code settings
echo "[5/6] Configuring Claude Code settings..."

if [ ! -f "$SETTINGS_FILE" ]; then
    echo '{}' > "$SETTINGS_FILE"
fi

python3 - "$SETTINGS_FILE" "$SCRIPT_DIR" << 'PYEOF'
import json
import sys

settings_file = sys.argv[1]
repo_dir = sys.argv[2]

with open(settings_file, "r") as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})

# The real hooks live in the vendored cortex/hooks/ directory.
# PYTHONPATH must include the cortex dir for imports to resolve.
cortex_dir = f"{repo_dir}/cortex"

# --- SessionStart hook (baton injection) ---
session_start = hooks.setdefault("SessionStart", [])
baton_hook_cmd = f"PYTHONPATH={cortex_dir}:$PYTHONPATH python3 {cortex_dir}/hooks/baton_hook.py"
already_has = any(
    "baton_hook" in h.get("command", "")
    for entry in session_start
    for h in entry.get("hooks", [])
)
if not already_has:
    session_start.append({
        "hooks": [{
            "type": "command",
            "command": baton_hook_cmd,
            "timeout": 20,
        }]
    })
    print("  Added SessionStart hook")
else:
    print("  SessionStart hook already present")

# --- PreCompact hook (session persistence) ---
pre_compact = hooks.setdefault("PreCompact", [])
compact_hook_cmd = f"PYTHONPATH={cortex_dir}:$PYTHONPATH python3 {cortex_dir}/hooks/compact_hook.py"
already_has = any(
    "compact_hook" in h.get("command", "")
    for entry in pre_compact
    for h in entry.get("hooks", [])
)
if not already_has:
    pre_compact.append({
        "matcher": ".*",
        "hooks": [{
            "type": "command",
            "command": compact_hook_cmd,
            "timeout": 30,
        }]
    })
    print("  Added PreCompact hook")
else:
    print("  PreCompact hook already present")

# --- Statusline ---
statusline_cmd = "python3 ~/.claude/hooks/baton-statusline.py"
current_statusline = settings.get("statusLine", {})
if current_statusline.get("command") != statusline_cmd:
    settings["statusLine"] = {
        "type": "command",
        "command": statusline_cmd,
    }
    print("  Set statusline to baton-statusline.py")
else:
    print("  Statusline already configured")

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

PYEOF

# 6. Verify
echo "[6/6] Verifying installation..."
echo ""

CHECKS=0
TOTAL=5

check() {
    if eval "$2"; then
        echo "  [OK] $1"
        CHECKS=$((CHECKS + 1))
    else
        echo "  [FAIL] $1"
    fi
}

check "baton-statusline.py installed"       "[ -f '$HOOKS_DIR/baton-statusline.py' ]"
check "cortex code linked"                   "[ -L '$BATON_DIR/cortex' ] || [ -d '$BATON_DIR/cortex' ]"
check "hypervisa code linked"                "[ -L '$BATON_DIR/hypervisa' ] || [ -d '$BATON_DIR/hypervisa' ]"
check "cxdb_client importable"              "python3 -c 'import sys; sys.path.insert(0,\"$SCRIPT_DIR/cortex\"); import cxdb_client' 2>/dev/null"
check "hypervisa.baton importable"          "PYTHONPATH=$SCRIPT_DIR:$PYTHONPATH python3 -c 'from hypervisa.baton import synthesize_baton' 2>/dev/null"

echo ""
echo "=== Installation complete ($CHECKS/$TOTAL checks passed) ==="
echo ""
echo "Required services:"
echo "  - cxdb:          systemctl start cxdb         (ports 9009/9010)"
echo "  - HyperVisa API: hypervisa-api                (port 8042)"
echo "  - Gemini key:    export GEMINI_API_KEY=...     (for baton synthesis)"
echo ""
echo "Baton Exchange will activate on your next Claude Code session."
