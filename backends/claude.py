"""Claude Code backend for agent-command-center.

Reads session state from ~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl —
one JSONL file per session, with one event per line. Each line is a JSON object
with type, timestamp, sessionId, cwd, gitBranch, version, and (for assistant
events) message.usage with token counts.

Exports the public backend interface:
- gather_sessions() -> dict
- gather_token_timeseries() -> dict
- focus_terminal_for_pid(pid) -> dict

PID-to-session mapping note: Claude Code doesn't expose a clean PID→session
mapping the way Copilot does (Copilot encodes the PID in its log file name).
For v1 we list sessions by recent JSONL mtime — every JSONL modified within
ACTIVE_THRESHOLD_SECONDS is treated as an active session. PID-dependent
features (kill, focus terminal) work on a best-effort basis: we attempt to
match a claude.exe PID by cwd, and fall back to skipping if no match.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# A JSONL whose mtime is within this many seconds of "now" is considered active.
# 6 hours catches "session I had open all afternoon" without flooding with stale ones.
ACTIVE_THRESHOLD_SECONDS = 6 * 60 * 60

# Token usage cache — avoid re-scanning unchanged JSONLs.
# Key: jsonl_path -> {"size": int, "tokens": dict}
_token_cache = {}

# Timeseries cache — offset-based incremental scanning.
# Key: jsonl_path -> {"offset": int, "buckets": {minute_key: token_dict}}
_timeseries_cache = {}


# -----------------------------------------------------------------------------
# JSONL discovery + parsing
# -----------------------------------------------------------------------------

def find_session_files():
    """Return all session JSONL files across all project directories."""
    if not PROJECTS_DIR.exists():
        return []
    return list(PROJECTS_DIR.glob("*/*.jsonl"))


def find_active_session_files(threshold_seconds=ACTIVE_THRESHOLD_SECONDS):
    """Return JSONLs modified within the active-session threshold."""
    now = datetime.now(tz=timezone.utc).timestamp()
    active = []
    for jsonl in find_session_files():
        try:
            if now - os.path.getmtime(jsonl) <= threshold_seconds:
                active.append(jsonl)
        except OSError:
            pass
    active.sort(key=os.path.getmtime, reverse=True)
    return active


def iter_jsonl_events(jsonl_path):
    """Yield parsed JSON events from a JSONL file, skipping malformed lines."""
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


# Patterns that mark a "user message" as a system event rather than a real
# user prompt — we skip these when picking a session title or current intent.
_SYSTEM_TAG_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-args>",
    "<command-message>",
    "<system-reminder>",
    "<user-prompt-submit-hook>",
)


def _is_system_message(text):
    """Return True if `text` is a system marker (compaction notice, command
    metadata, image-paste metadata, CLI flag echo, etc.) and should NOT be
    treated as the user's real intent."""
    if not text:
        return True
    t = text.strip()
    if not t:
        return True
    # XML/HTML-style tag-prefix patterns Claude Code injects as "user role" messages
    for p in _SYSTEM_TAG_PREFIXES:
        if t.lower().startswith(p):
            return True
    # Image-paste metadata: "[Image: original 2727x1553, displayed at ..."
    if t.startswith("[Image:") or t.startswith("[File:"):
        return True
    # Compaction continuation markers
    if "This session is being continued from a previous conversation" in t:
        return True
    if t.startswith("Caveat: The messages below"):
        return True
    # CLI flag echoes ("--resume", "-c", "--continue some-id", etc.) — these
    # come from `claude --resume <id>` and aren't user intent.
    if t.startswith("--") or (t.startswith("-") and len(t) <= 40 and "\n" not in t):
        return True
    return False


def _extract_text(content):
    """Extract a plain-text snippet from a message's `content` field, which
    may be a string OR a list of typed blocks (text, tool_use, tool_result)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text") or ""
    return ""


def read_session_summary(jsonl_path):
    """Walk a JSONL and pull out summary fields: cwd, gitBranch, model, sessionId, etc."""
    summary = {
        "session_id": None,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "version": None,
        "first_timestamp": None,
        "last_timestamp": None,
        "first_user_message": None,    # first NON-system user message (best for title)
        "last_user_message": None,     # last NON-system user message (best for current intent)
        "last_assistant_event": None,
        "ai_title": None,
        "n_events": 0,
        "n_assistant_events": 0,
    }
    for ev in iter_jsonl_events(jsonl_path):
        summary["n_events"] += 1
        if not summary["session_id"]:
            summary["session_id"] = ev.get("sessionId")
        if not summary["cwd"] and ev.get("cwd"):
            summary["cwd"] = ev["cwd"]
        if not summary["git_branch"] and ev.get("gitBranch"):
            summary["git_branch"] = ev["gitBranch"]
        if not summary["version"] and ev.get("version"):
            summary["version"] = ev["version"]
        ts = ev.get("timestamp")
        if ts:
            if not summary["first_timestamp"]:
                summary["first_timestamp"] = ts
            summary["last_timestamp"] = ts

        etype = ev.get("type")
        msg = ev.get("message") or {}
        if etype == "user":
            text = _extract_text(msg.get("content"))
            if not _is_system_message(text):
                if not summary["first_user_message"]:
                    summary["first_user_message"] = text
                summary["last_user_message"] = text
        elif etype == "assistant":
            summary["n_assistant_events"] += 1
            summary["last_assistant_event"] = ev
            if msg.get("model"):
                summary["model"] = msg["model"]
        elif etype == "ai-title":
            title = ev.get("title") or msg.get("title") or ev.get("content")
            if isinstance(title, str) and title.strip():
                summary["ai_title"] = title.strip()

    if not summary["session_id"]:
        summary["session_id"] = jsonl_path.stem
    return summary


def _format_duration(seconds):
    """Format a duration in seconds as a short human string: '5s', '3m 12s', '4h 22m'."""
    if seconds is None or seconds < 0:
        return None
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _compute_session_age(first_ts, last_ts):
    """Return (age_seconds, age_label, idle_seconds, idle_label).

    age = first_ts → now
    idle = last_ts → now
    """
    age_s = idle_s = None
    if first_ts:
        try:
            first_dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            age_s = (datetime.now(tz=timezone.utc) - first_dt).total_seconds()
        except ValueError:
            pass
    if last_ts:
        try:
            last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            idle_s = (datetime.now(tz=timezone.utc) - last_dt).total_seconds()
        except ValueError:
            pass
    return age_s, _format_duration(age_s), idle_s, _format_duration(idle_s)


# -----------------------------------------------------------------------------
# Token usage
# -----------------------------------------------------------------------------

def extract_token_usage(jsonl_path):
    """Sum all token usage across assistant events. Cached by file size."""
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    try:
        file_size = os.path.getsize(jsonl_path)
        cache_key = str(jsonl_path)

        cached = _token_cache.get(cache_key)
        if cached and cached["size"] == file_size:
            return cached["tokens"]

        start_offset = 0
        if cached and cached["size"] < file_size:
            tokens = dict(cached["tokens"])
            start_offset = cached["size"]

        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            if start_offset > 0:
                f.seek(start_offset)
                f.readline()
            for line in f:
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "assistant":
                    continue
                usage = (ev.get("message") or {}).get("usage") or {}
                tokens["input"] += int(usage.get("input_tokens") or 0)
                tokens["output"] += int(usage.get("output_tokens") or 0)
                tokens["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                tokens["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)

        _token_cache[cache_key] = {"size": file_size, "tokens": dict(tokens)}
    except OSError:
        pass
    return tokens


def extract_context_window(summary):
    """The last assistant event's usage gives the current context window load.

    All three token types from `usage` are fed into the model and count toward
    the context window — even cache_read tokens occupy window space, they're
    just cheaper. Window = input + cache_creation + cache_read.

    The context window ceiling depends on the model. We use an empirical
    detection: if observed usage exceeds 200K, the model must be a 1M-context
    variant (Opus 4.7 1M, etc.) — otherwise the API call would have errored.

    `cache_read` is also reported separately so the dashboard can show
    "of the X tokens in context, Y were served from cache."

    Returns {used, max, percent, cache_read} or None.
    """
    last_ev = summary.get("last_assistant_event")
    if not last_ev:
        return None
    usage = (last_ev.get("message") or {}).get("usage") or {}
    fresh_input = int(usage.get("input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    used = fresh_input + cache_creation + cache_read
    if used == 0:
        return None
    model = (summary.get("model") or "").lower()
    # Empirical: if usage > 200K, model must be a 1M-context variant.
    # Also detect known 1M-tier model names.
    if used > 200_000 or "1m" in model or "opus-4-7" in model:
        max_context = 1_000_000
    else:
        max_context = 200_000
    return {
        "used": used,
        "max": max_context,
        "percent": round(used / max_context * 100, 1),
        "cache_read": cache_read,
    }


# -----------------------------------------------------------------------------
# Activity + status
# -----------------------------------------------------------------------------

def get_log_activity(jsonl_path):
    """Determine working/idle status from the JSONL's mtime."""
    try:
        mtime = os.path.getmtime(jsonl_path)
        last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        age = (datetime.now(tz=timezone.utc) - datetime.fromtimestamp(mtime, tz=timezone.utc)).total_seconds()
        status = "working" if age < 30 else "idle"
        return {"status": status, "last_activity": last_activity}
    except OSError:
        return {"status": "unknown", "last_activity": None}


def detect_waiting_for_user(summary):
    """Waiting = last assistant event has stop_reason=end_turn, no pending tool calls."""
    last_ev = summary.get("last_assistant_event")
    if not last_ev:
        return False
    msg = last_ev.get("message") or {}
    if msg.get("stop_reason") != "end_turn":
        return False
    # If the last assistant event contains tool_use blocks without tool_result follow-up,
    # the model is mid-turn (not waiting for user). The simpler heuristic: end_turn alone
    # is a strong signal we're awaiting the next user message.
    return True


def extract_activity_feed(jsonl_path, session_name, n=10):
    """Recent events from the JSONL, formatted as activity items."""
    events = []
    for ev in iter_jsonl_events(jsonl_path):
        ts = ev.get("timestamp")
        if not ts:
            continue
        etype = ev.get("type")
        msg = ev.get("message") or {}

        if etype == "user":
            content = msg.get("content")
            text = None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        break
            if text:
                preview = text[:80] + ("…" if len(text) > 80 else "")
                events.append({"time": ts, "type": "user_message",
                               "message": f"User: {preview}", "session": session_name})

        elif etype == "assistant":
            content = msg.get("content")
            tool_names = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_names.append(block.get("name", "?"))
            if tool_names:
                events.append({"time": ts, "type": "tools",
                               "message": f"Called {len(tool_names)} tool(s): {', '.join(tool_names[:3])}",
                               "session": session_name})
            if msg.get("stop_reason") == "end_turn":
                events.append({"time": ts, "type": "turn_complete",
                               "message": "Turn completed", "session": session_name})

    events.sort(key=lambda e: e["time"], reverse=True)
    return events[:n]


# -----------------------------------------------------------------------------
# PID mapping (best-effort)
# -----------------------------------------------------------------------------

def get_claude_pids():
    """Get all running claude.exe PIDs with their parent IDs."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
             "Select-Object ProcessId, ParentProcessId, CommandLine | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception:
        return []


def session_id_to_pid_best_effort(session_id):
    """Try to match a claude.exe PID to a session ID via the command line.

    Claude Code processes typically embed the project path in their command line.
    This is best-effort — if no match, returns None and the session shows without
    a PID (kill/focus features disabled for that row).
    """
    procs = get_claude_pids()
    # Look for the session ID anywhere in the command line — it appears as a
    # --session-id flag when a session is resumed, but for fresh sessions we
    # can't tie a session to a PID reliably without OS-level handle inspection.
    for p in procs:
        cmd = (p.get("CommandLine") or "")
        if session_id in cmd:
            return p.get("ProcessId")
    return None


# -----------------------------------------------------------------------------
# Public interface
# -----------------------------------------------------------------------------

def _pick_session_title(summary):
    """Pick the best display title for a session.

    Priority:
      1. ai-title (auto-generated by Claude Code when present)
      2. First real user message (truncated) — better than the last because
         the last is often a one-word follow-up ("yes", "push", "ok")
      3. "Unnamed Session"
    """
    title = summary.get("ai_title")
    if title:
        return title
    first = summary.get("first_user_message")
    if first:
        first = first.strip()
        if len(first) > 60:
            return first[:60].rstrip() + "…"
        return first
    return "Unnamed Session"


def gather_sessions():
    """Main aggregator — return all recently-active Claude Code sessions."""
    sessions = []
    for jsonl in find_active_session_files():
        summary = read_session_summary(jsonl)
        if summary["n_events"] == 0:
            continue

        activity = get_log_activity(jsonl)
        token_usage = extract_token_usage(jsonl)
        waiting_for_user = detect_waiting_for_user(summary)
        context_window = extract_context_window(summary)
        age_s, age_label, idle_s, idle_label = _compute_session_age(
            summary.get("first_timestamp"), summary.get("last_timestamp")
        )

        session_name = _pick_session_title(summary)

        status = activity["status"]
        if waiting_for_user and status == "idle":
            status = "waiting"

        pid = session_id_to_pid_best_effort(summary["session_id"])
        session_activity = extract_activity_feed(jsonl, session_name)

        sessions.append({
            "session_id": summary["session_id"],
            "pid": pid,  # may be None
            "name": session_name,
            "current_intent": summary.get("last_user_message"),
            "cwd": summary.get("cwd") or "Unknown",
            "repository": None,
            "branch": summary.get("git_branch"),
            "host_type": "claude-code",
            "created_at": summary.get("first_timestamp"),
            "updated_at": summary.get("last_timestamp"),
            "session_age_seconds": age_s,
            "session_age_label": age_label,
            "idle_seconds": idle_s,
            "idle_label": idle_label,
            "status": status,
            "last_activity": activity["last_activity"],
            "turn_count": summary["n_assistant_events"],
            "checkpoint_count": 0,
            "checkpoints": [],
            "token_usage": token_usage,
            "context_window": context_window,
            "activity_feed": session_activity,
            "remote_steerable": False,
            "model": summary.get("model"),
            "version": summary.get("version"),
        })

    return {"sessions": sessions}


def extract_token_timeseries(jsonl_path):
    """Token usage per minute from a JSONL. Cached by file offset."""
    if jsonl_path is None:
        return {}
    cache_key = str(jsonl_path)
    cached = _timeseries_cache.get(cache_key, {"offset": 0, "buckets": {}})

    try:
        file_size = os.path.getsize(jsonl_path)
        if file_size <= cached["offset"]:
            return cached["buckets"]

        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            if cached["offset"] > 0:
                f.seek(cached["offset"])
                f.readline()
            for line in f:
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "assistant":
                    continue
                ts = ev.get("timestamp")
                if not ts or len(ts) < 16:
                    continue
                minute_key = ts[:16]  # "YYYY-MM-DDTHH:MM"
                usage = (ev.get("message") or {}).get("usage") or {}
                bucket = cached["buckets"].setdefault(minute_key, {
                    "input": 0, "output": 0, "cache_read": 0, "cache_write": 0
                })
                bucket["input"] += int(usage.get("input_tokens") or 0)
                bucket["output"] += int(usage.get("output_tokens") or 0)
                bucket["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                bucket["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)

        cached["offset"] = file_size
        _timeseries_cache[cache_key] = cached
    except OSError:
        pass
    return cached["buckets"]


def gather_token_timeseries():
    """Token timeseries merged across all active sessions, per-minute."""
    merged = {}
    for jsonl in find_active_session_files():
        buckets = extract_token_timeseries(jsonl)
        for minute_key, counts in buckets.items():
            if minute_key not in merged:
                merged[minute_key] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}
            for t in ("input", "output", "cache_read", "cache_write"):
                merged[minute_key][t] += counts.get(t, 0)
                merged[minute_key]["total"] += counts.get(t, 0)

    sorted_minutes = sorted(merged.keys())
    if not sorted_minutes:
        return {"minutes": [], "series": {"input": [], "output": [], "cache_read": [], "cache_write": [], "total": []}}

    result_minutes = []
    series = {"input": [], "output": [], "cache_read": [], "cache_write": [], "total": []}

    first = datetime.strptime(sorted_minutes[0], "%Y-%m-%dT%H:%M")
    last = datetime.strptime(sorted_minutes[-1], "%Y-%m-%dT%H:%M")
    current = first
    while current <= last:
        key = current.strftime("%Y-%m-%dT%H:%M")
        result_minutes.append(key)
        data = merged.get(key, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0})
        for t in ("input", "output", "cache_read", "cache_write", "total"):
            series[t].append(data[t])
        current += timedelta(minutes=1)

    return {"minutes": result_minutes, "series": series}


def focus_terminal_for_pid(target_pid):
    """Bring the terminal window hosting a claude.exe PID to foreground.

    Same approach as Copilot — walk the process tree to find a WindowsTerminal
    or fallback shell window. Reused verbatim since it's not agent-specific.
    """
    # Import lazily — focus is the same algorithm regardless of agent,
    # so we delegate to the copilot backend's implementation.
    from . import copilot
    return copilot.focus_terminal_for_pid(target_pid)
