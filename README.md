# Agent Command Center

Real-time monitoring dashboard for AI coding agent sessions. Supports **GitHub Copilot CLI** and **Claude Code**.

For each active session it shows:
- Current intent / last user message
- Token usage (cumulative + per-minute timeseries)
- Context window load
- Activity feed (recent tool calls, turn completions)
- Working / idle / waiting-for-user status
- Working directory, git branch
- Quick actions: focus the terminal window, kill the session

No external dependencies — Python stdlib only.

## Quick start

**Copilot CLI** (default):

```
start-command-center.bat
```

**Claude Code:**

```
start-command-center-claude.bat
```

Both open `http://localhost:9876/` in your default browser.

Or run directly:

```bash
# Copilot
python command-center-api.py

# Claude Code
AGENT_TYPE=claude python command-center-api.py
```

On Windows cmd.exe:

```
set AGENT_TYPE=claude && python command-center-api.py
```

## How it works

The HTTP/JSON API and the dashboard HTML are agent-agnostic. Each backend produces sessions in the same shape so the frontend stays one codebase.

### Copilot backend (`backends/copilot.py`)

Data source: `~/.copilot/`
- `logs/process-<pid>-<session>.log` — session log files
- `session-state/<session>/workspace.yaml` — workspace metadata
- `session-store.db` — SQLite store with turns + checkpoints

Detects sessions by listing `copilot.exe` processes and matching them to log files.

### Claude Code backend (`backends/claude.py`)

Data source: `~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl`

Each JSONL is one session, with one event per line:
- `type: "user"` — user messages (treated as current intent)
- `type: "assistant"` — model responses with `usage` tokens, `model`, `stop_reason`
- `type: "queue-operation"` — queued operations
- `type: "ai-title"` — auto-generated session title (used as session name)

Sessions are listed by recent JSONL `mtime` (default: last 6 hours). PID-to-session mapping is best-effort via command-line inspection — sessions still display without a PID if the match fails (kill/focus features depend on the PID being known).

## Endpoints

| Endpoint | Method | Returns |
|---|---|---|
| `/` | GET | Dashboard HTML |
| `/api/sessions` | GET | List of active sessions with details |
| `/api/token-timeseries` | GET | Per-minute token buckets across all sessions |
| `/api/agent-info` | GET | Currently-selected backend (`copilot` or `claude`) |
| `/api/open-folder?path=...` | GET | Opens the given folder in Explorer |
| `/api/kill-session` | POST `{pid}` | Sends SIGTERM to a session PID |
| `/api/focus-terminal` | POST `{pid}` | Brings the terminal hosting the PID to foreground |

## Files

```
command-center-api.py        Thin HTTP server — dispatches to active backend
command-center.html          Dashboard UI (agent-agnostic)
backends/
  __init__.py
  copilot.py                 Copilot CLI session reader
  claude.py                  Claude Code session reader
start-command-center.bat         Launcher (Copilot, default)
start-command-center-claude.bat  Launcher (Claude Code)
```

## Notes

- Both backends are Windows-first (process detection + window focus use PowerShell + Win32). The session-reading parts work on any OS; the process and focus features need Windows.
- Tokens shown reflect what the backend can parse — for Claude that's `input_tokens`, `output_tokens`, `cache_read_input_tokens`, and `cache_creation_input_tokens` from each assistant event's `usage` field.
- "Context window" for Claude defaults to 200K; switches to 1M if the session's model name contains `"1m"`.
- Sessions older than 6 hours don't appear (Claude) or sessions whose process has exited don't appear (Copilot). Adjust `ACTIVE_THRESHOLD_SECONDS` in `backends/claude.py` if you want a wider window.
