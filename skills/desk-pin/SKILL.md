---
name: desk-pin
description: |
  Pin the current Claude Code conversation to the Claude Desk dashboard so it appears in the Pinned section on the home page. Use when the user says "desk pin", "pin this to claude desk", "pin to desk", "save this to desk", or "/desk-pin". Accepts an optional label — if provided, pins with that name; otherwise prompts the user for a label. Different from /pin which saves to Notion.
user-invokable: true
args: "<optional label>"
---

# Skill: desk-pin

## Trigger
User runs `/desk-pin` or `/desk-pin <label>`

## What it does
Pins the current Claude Code conversation to the Claude Desk dashboard (http://localhost:8080) so it appears in the "Pinned" section on the home page with a custom label.

This is different from `/pin`, which saves conversation summaries to Notion. `/desk-pin` just marks the session as pinned in claude-desk's favorites store so you can jump back to it quickly.

## Step 1 — Determine the label

- If the user provided a label as an argument (e.g. `/desk-pin architecture refactor`), use that.
- If no label was provided, ask the user: "What should I name this pin? (leave empty to pin without a label)"
- Wait for the user's response before continuing.

## Step 2 — Find the current session ID

Use the Bash tool to find the session ID of the currently active Claude Code session:

```bash
find ~/.claude/projects -name "*.jsonl" -type f -not -name "agent-*" -mmin -10 -exec stat -f "%m %N" {} \; 2>/dev/null | sort -rn | head -1 | awk '{print $2}' | xargs -I {} basename {} .jsonl
```

This finds the JSONL file modified most recently (within the last 10 minutes) and extracts the session ID from the filename. Since you are actively in this conversation right now, the most recently modified JSONL will be the current session.

If nothing is returned (e.g. in a fresh session that hasn't written to disk yet), fall back to:

```bash
find ~/.claude/projects -name "*.jsonl" -type f -not -name "agent-*" -exec stat -f "%m %N" {} \; 2>/dev/null | sort -rn | head -1 | awk '{print $2}' | xargs -I {} basename {} .jsonl
```

Store the result as `SESSION_ID`.

## Step 3 — Call the claude-desk API

POST to the pin endpoint using curl:

```bash
curl -s -X POST "http://localhost:8080/api/favorites/${SESSION_ID}/pin" \
  -H "Content-Type: application/json" \
  -d "{\"label\": \"<the label from step 1>\"}"
```

Make sure to JSON-escape the label (quotes, backslashes, newlines). Use `jq -Rs .` or similar if the label might contain special characters:

```bash
LABEL_JSON=$(printf '%s' "$LABEL" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
curl -s -X POST "http://localhost:8080/api/favorites/${SESSION_ID}/pin" \
  -H "Content-Type: application/json" \
  -d "{\"label\": ${LABEL_JSON}}"
```

## Step 4 — Confirm to user

On success, respond with one line, for example:
- `Pinned as "architecture refactor" → http://localhost:8080`
- `Pinned (no label) → http://localhost:8080`

On failure (non-200 response, connection refused, etc.), report the specific error:
- If connection refused: "Claude Desk isn't running. Start it with `launchctl load ~/Library/LaunchAgents/com.claude.desk.plist` or check `~/claude-desk/claude-desk.log`."
- If 404: "Session not found in claude-desk. It may not have been archived yet — try again in a minute."
- Otherwise: show the raw response body.

## Notes

- Renaming: to rename an existing pin, just run `/desk-pin <new label>` again in the same session. The endpoint handles both create and update.
- Unpinning: not supported via this skill. Unpin from the claude-desk UI by clicking the star.
- The pin persists in `~/.claude/session-favorites.json` — the same store the claude-desk UI uses.
