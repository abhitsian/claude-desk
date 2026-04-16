#!/bin/bash
#
# Claude Desk — Install Script
#
# Sets up:
#   1. Python dependencies
#   2. Dashboard server (LaunchAgent, auto-start on login)
#   3. Daily archiver cron (LaunchAgent, runs at 3 AM)
#   4. Initial archive of all current sessions
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(which python3)"
PLIST_DIR="$HOME/Library/LaunchAgents"

echo "Claude Desk — Install"
echo "================================"
echo ""
echo "Directory: $SCRIPT_DIR"
echo "Python:    $PYTHON"
echo "Home:      $HOME"
echo ""

# 1. Install dependencies
echo "Installing Python dependencies..."
pip3 install -q fastapi uvicorn jinja2 python-multipart pydantic pydantic-settings aiofiles 2>/dev/null
echo "  Done."

# 2. Generate dashboard LaunchAgent
DASHBOARD_PLIST="$PLIST_DIR/com.claude.desk.plist"
echo "Creating dashboard LaunchAgent..."

cat > "$DASHBOARD_PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.desk</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>claude_sessions.main:app</string>
        <string>--port</string>
        <string>8080</string>
        <string>--host</string>
        <string>127.0.0.1</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude-desk.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-desk.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

launchctl unload "$DASHBOARD_PLIST" 2>/dev/null || true
launchctl load "$DASHBOARD_PLIST"
echo "  Dashboard running at http://localhost:8080"

# 3. Generate archiver LaunchAgent
ARCHIVER_PLIST="$PLIST_DIR/com.claude.session-archiver.plist"
echo "Creating daily archiver LaunchAgent..."

cat > "$ARCHIVER_PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.session-archiver</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/archive_cron.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/claude-archiver.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-archiver.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

launchctl unload "$ARCHIVER_PLIST" 2>/dev/null || true
launchctl load "$ARCHIVER_PLIST"
echo "  Archiver scheduled (daily at 3 AM)"

# 4. Install Claude Code skills
if [ -d "$SCRIPT_DIR/skills" ]; then
    echo "Installing Claude Code skills..."
    SKILLS_DEST="$HOME/.claude/skills"
    mkdir -p "$SKILLS_DEST"
    for skill_dir in "$SCRIPT_DIR/skills"/*/; do
        skill_name=$(basename "$skill_dir")
        mkdir -p "$SKILLS_DEST/$skill_name"
        cp -R "$skill_dir"* "$SKILLS_DEST/$skill_name/"
        echo "  Installed /$skill_name"
    done
fi

# 5. Initial archive
echo ""
echo "Running initial archive of all sessions..."
cd "$SCRIPT_DIR"
$PYTHON archive_cron.py --all

echo ""
echo "================================"
echo "Install complete!"
echo ""
echo "  Dashboard:  http://localhost:8080"
echo "  Archive:    Runs daily at 3 AM"
echo "  Data:       ~/.claude/session-archive.db"
echo ""
echo "To uninstall:"
echo "  launchctl unload ~/Library/LaunchAgents/com.claude.desk.plist"
echo "  launchctl unload ~/Library/LaunchAgents/com.claude.session-archiver.plist"
echo ""
