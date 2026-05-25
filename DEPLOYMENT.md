# Deployment & operations (READ THIS FIRST)

**There is ONE codebase.** GitHub `zamabama/whatsapp-mcp` (`main`) is the single source
of truth. It is deployed twice on this machine to serve two WhatsApp accounts. The
*code* is identical in both; only **runtime config + data** differ.

> If you are an agent about to "fix" the bridge: edit the code, commit, push to `main`,
> then `git pull` in **both** deployment dirs. Do **not** hand-edit one copy and assume
> it's "the" bridge — that is how this drifted before.

## The two deployments

| | Personal | Business ("garuda") |
|---|---|---|
| Code dir | `~/.claude/mcp-servers/whatsapp-mcp-personal` | `~/.claude/mcp-servers/whatsapp-mcp` |
| Account/data | `whatsapp-bridge/store/` (gitignored) | `whatsapp-bridge/store/` (gitignored) |
| Port | **8180** | **8179** |
| LaunchAgent | `~/Library/LaunchAgents/com.zam.whatsapp-personal.plist` | `~/Library/LaunchAgents/com.garuda.whatsapp-bridge.plist` |
| Used by (`.mcp.json`) | global `~/.claude`, Cocobun, Glowup, bali-biz | primogen, peptide-biz |

Both git clones share the same `origin` (`zamabama/whatsapp-mcp`) and `upstream`
(`lharries/whatsapp-mcp`).

## What is code vs. per-deployment

- **Code (committed, identical in both):** `whatsapp-bridge/*.go`, `whatsapp-mcp-server/*.py`.
- **Per-deployment (NOT code):**
  - `whatsapp-bridge/store/` — the WhatsApp account session (`whatsapp.db`), the message
    DB (`messages.db`), media, and `bridge_port.txt`. All gitignored.
  - The port, set via the `WHATSAPP_BRIDGE_PORT` env var in each LaunchAgent plist.

### How the port flows (no per-project config needed)

1. The LaunchAgent plist sets `WHATSAPP_BRIDGE_PORT` (8180 / 8179).
2. The bridge listens on it and writes it to `store/bridge_port.txt`.
3. The MCP reader (`whatsapp.py`) resolves its port from `WHATSAPP_BRIDGE_PORT` →
   else `store/bridge_port.txt` → else `8080`. So `.mcp.json` files need **no** port/env.

## Update both deployments (after pushing to `main`)

```sh
for D in whatsapp-mcp-personal whatsapp-mcp; do
  cd ~/.claude/mcp-servers/$D
  git pull --ff-only origin main
  (cd whatsapp-bridge && go build -o whatsapp-bridge.new .)
done
# Personal:
P=~/Library/LaunchAgents/com.zam.whatsapp-personal.plist
cd ~/.claude/mcp-servers/whatsapp-mcp-personal/whatsapp-bridge
launchctl unload "$P"; mv whatsapp-bridge.new whatsapp-bridge; launchctl load "$P"
# Business:
G=~/Library/LaunchAgents/com.garuda.whatsapp-bridge.plist
cd ~/.claude/mcp-servers/whatsapp-mcp/whatsapp-bridge
launchctl unload "$G"; mv whatsapp-bridge.new whatsapp-bridge; launchctl load "$G"
```

Restarting drops that account's live WhatsApp connection for ~5–15s; it reconnects
automatically (`KeepAlive=true`) and needs **no** QR re-auth (session is in `store/`).

## Verify

```sh
curl -s localhost:8180/api/health   # personal
curl -s localhost:8179/api/health   # business
# expect: {"connected":true,"logged_in":true,"status":"ok","version":"1.2.0-..."}
```

## Rollback

- Each deployment keeps a pre-change binary backup next to it (gitignored
  `whatsapp-bridge.*`, e.g. `whatsapp-bridge.pre-ordering-fix`,
  `whatsapp-bridge.pre-consolidation`). To roll back: `launchctl unload`, copy the
  backup over `whatsapp-bridge`, `launchctl load`.
- The business clone's pre-consolidation working-tree edits are saved in `git stash`
  (`git stash list`).

## Behaviour notes (1.2.0-message-ordering)

- The bridge writes the chat row and the message row in **one transaction**
  (`StoreChatAndMessage`), so an external reader never sees a chat whose
  `last_message_time` is newer than its newest stored message.
- The DB contains two timestamp text formats (space- vs `T`-separated, from the Go
  bridge vs the Python outbound logger). Chronological ordering is normalised on the
  **read** side — `whatsapp.py` wraps every comparison/`ORDER BY` in `datetime()`. Do
  not add timestamp-format comparisons to the Go write path.
