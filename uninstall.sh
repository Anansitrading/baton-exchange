#!/usr/bin/env bash
set -euo pipefail

# Baton Exchange — Uninstaller
# Removes hooks, symlinks, and statusline. Preserves baton state data.

CLAUDE_DIR="$HOME/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
BATON_DIR="$HOME/.cortex/baton"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"

echo "=== Baton Exchange Uninstaller ==="
echo ""

# 1. Remove statusline
echo "[1/4] Removing statusline..."
rm -f "$HOOKS_DIR/baton-statusline.py"

# 2. Remove symlinks (preserve state files)
echo "[2/4] Removing code symlinks..."
rm -f "$BATON_DIR/cortex" "$BATON_DIR/hypervisa" 2>/dev/null || true

# 3. Clean settings
echo "[3/4] Cleaning Claude Code settings..."

if [ -f "$SETTINGS_FILE" ]; then
    python3 - "$SETTINGS_FILE" << 'PYEOF'
import json
import sys

settings_file = sys.argv[1]

with open(settings_file, "r") as f:
    settings = json.load(f)

hooks = settings.get("hooks", {})

# Remove baton hooks
for key in ("SessionStart", "PreCompact"):
    entries = hooks.get(key, [])
    hooks[key] = [
        entry for entry in entries
        if not any("baton" in h.get("command", "") for h in entry.get("hooks", []))
    ]
    if not hooks[key]:
        del hooks[key]

# Reset statusline if it's ours
sl = settings.get("statusLine", {})
if "baton-statusline" in sl.get("command", ""):
    del settings["statusLine"]
    print("  Removed statusline")

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("  Cleaned settings.json")

PYEOF
fi

echo "[4/4] Done."
echo ""
echo "Baton state data preserved at ~/.cortex/baton/"
echo "  (project-contexts.json, baton-*.json, last-inject.json)"
echo "To remove all state: rm -rf ~/.cortex/baton/"
echo ""
echo "=== Uninstall complete ==="
