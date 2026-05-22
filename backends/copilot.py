"""GitHub Copilot CLI backend for agent-command-center.

Reads session state from ~/.copilot/ — logs, session-state YAMLs, and the
session-store.db SQLite. Detects copilot.exe processes and maps them to
session IDs by parsing log file names.

Exports the public backend interface:
- gather_sessions() -> dict
- gather_token_timeseries() -> dict
- focus_terminal_for_pid(pid) -> dict
"""

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timezone

COPILOT_DIR = Path.home() / ".copilot"
LOGS_DIR = COPILOT_DIR / "logs"
SESSION_STATE_DIR = COPILOT_DIR / "session-state"
SESSION_STORE_DB = COPILOT_DIR / "session-store.db"

# How many bytes from the end of log to read for recent activity
LOG_TAIL_BYTES = 200000

# Cache for token counts (avoid re-scanning entire logs every refresh)
_token_cache = {}

# Cache for timeseries data (offset-based incremental scanning)
_timeseries_cache = {}


def get_copilot_pids():
    """Get PIDs of running copilot.exe main processes (children, not launchers)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='copilot.exe'\" | "
             "Select-Object ProcessId, ParentProcessId | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]

        all_pids = {p["ProcessId"] for p in data}
        main_pids = []
        for p in data:
            if p["ParentProcessId"] in all_pids:
                main_pids.append(p["ProcessId"])
        return main_pids
    except Exception:
        return []


def pid_to_session_id(pid):
    """Find the session ID for a given copilot PID by matching its log file."""
    pattern = re.compile(rf"process-\d+-{pid}\.log$")
    if not LOGS_DIR.exists():
        return None, None
    for log_file in LOGS_DIR.iterdir():
        if pattern.match(log_file.name):
            try:
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f):
                        if i > 100:
                            break
                        m = re.search(r"Workspace initialized: ([0-9a-f-]{36})", line)
                        if m:
                            return m.group(1), log_file
            except Exception:
                pass
    return None, None


def read_log_tail(log_file, num_bytes=LOG_TAIL_BYTES):
    """Read the last N bytes of a log file."""
    if log_file is None:
        return ""
    try:
        size = os.path.getsize(log_file)
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            if size > num_bytes:
                f.seek(size - num_bytes)
                f.readline()  # skip partial line
            return f.read()
    except Exception:
        return ""


def extract_current_intent(log_tail):
    """Extract the most recent report_intent from log tail."""
    matches = re.findall(
        r'"name":\s*"report_intent"[^}]{0,200}?"intent(?:\\"|"):\s*(?:\\"|")([^"\\]+)',
        log_tail
    )
    if matches:
        return matches[-1]
    return None


def extract_context_window(log_tail):
    """Extract the most recent turn's context window usage from log tail."""
    max_context = 200000  # Claude context window in Copilot
    last_turn = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    pending_count = None
    in_usage_block = False
    current_turn = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    for line in log_tail.split("\n"):
        if "copilot_usage" in line:
            in_usage_block = True
            current_turn = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        if in_usage_block:
            if '"token_count"' in line:
                m = re.search(r'"token_count":\s*(\d+)', line)
                if m:
                    pending_count = int(m.group(1))
            elif '"token_type"' in line and pending_count is not None:
                m = re.search(r'"token_type":\s*"(input|output|cache_read|cache_write)"', line)
                if m:
                    current_turn[m.group(1)] = pending_count
                pending_count = None
            elif "total_nano_aiu" in line or (line.strip().startswith("}") and line.strip().endswith("}")):
                if any(v > 0 for v in current_turn.values()):
                    last_turn = current_turn
                in_usage_block = False
                pending_count = None
        else:
            pending_count = None

    used = last_turn["input"] + last_turn["cache_read"] + last_turn["cache_write"]
    if used == 0:
        return None
    return {
        "used": used,
        "max": max_context,
        "percent": round(used / max_context * 100, 1)
    }


def extract_token_usage(log_file):
    """Extract cumulative token usage by streaming through the entire log file."""
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    if log_file is None:
        return tokens
    try:
        file_size = os.path.getsize(log_file)
        cache_key = str(log_file)

        cached = _token_cache.get(cache_key)
        if cached and cached["size"] == file_size:
            return cached["tokens"]

        start_offset = 0
        if cached and cached["size"] < file_size:
            tokens = dict(cached["tokens"])
            start_offset = cached["size"]

        pending_count = None
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            if start_offset > 0:
                f.seek(start_offset)
                f.readline()
            for line in f:
                if '"token_count"' in line:
                    m = re.search(r'"token_count":\s*(\d+)', line)
                    if m:
                        pending_count = int(m.group(1))
                elif '"token_type"' in line and pending_count is not None:
                    m = re.search(r'"token_type":\s*"(input|output|cache_read|cache_write)"', line)
                    if m:
                        tokens[m.group(1)] += pending_count
                    pending_count = None
                else:
                    if '"token_type"' not in line and '"token_count"' not in line:
                        pending_count = None

        _token_cache[cache_key] = {"size": file_size, "tokens": dict(tokens)}
    except Exception:
        pass
    return tokens


def detect_waiting_for_user(log_tail, log_file):
    """Detect if session is waiting for user input."""
    end_of_group_matches = re.findall(
        r"(\d{4}-\d{2}-\d{2}T[\d:.]+Z) \[INFO\] --- End of group ---",
        log_tail
    )
    tool_matches = re.findall(
        r"(\d{4}-\d{2}-\d{2}T[\d:.]+Z) \[DEBUG\] (?:Tool calls count|Running tool)",
        log_tail
    )

    if not end_of_group_matches:
        return False

    last_end = end_of_group_matches[-1]
    if not tool_matches:
        return True

    last_tool = tool_matches[-1]
    return last_end > last_tool


def extract_activity_feed(log_tail, session_name):
    """Extract recent activity events from log tail."""
    events = []

    for m in re.finditer(
        r"(\d{4}-\d{2}-\d{2}T[\d:.]+Z) \[DEBUG\] Tool calls count: (\d+)",
        log_tail
    ):
        events.append({
            "time": m.group(1),
            "type": "tools",
            "message": f"Executed {m.group(2)} tool call(s)",
            "session": session_name
        })

    for m in re.finditer(
        r"(\d{4}-\d{2}-\d{2}T[\d:.]+Z) \[INFO\] --- End of group ---",
        log_tail
    ):
        events.append({
            "time": m.group(1),
            "type": "turn_complete",
            "message": "Turn completed",
            "session": session_name
        })

    for m in re.finditer(r'"name":\s*"report_intent"[^}]{0,200}?"intent(?:\\\\"|"):\s*(?:\\\\"|")([^"\\\\]+)', log_tail):
        start = max(0, m.start() - 500)
        preceding = log_tail[start:m.start()]
        ts_match = re.findall(r"(\d{4}-\d{2}-\d{2}T[\d:.]+Z)", preceding)
        if ts_match:
            events.append({
                "time": ts_match[-1],
                "type": "intent",
                "message": f"Intent: {m.group(1)}",
                "session": session_name
            })

    events.sort(key=lambda e: e["time"], reverse=True)
    return events[:10]


def read_workspace_yaml(session_id):
    """Read workspace.yaml for a session and return parsed fields."""
    ws_path = SESSION_STATE_DIR / session_id / "workspace.yaml"
    if not ws_path.exists():
        return {}
    fields = {}
    try:
        with open(ws_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip()
    except Exception:
        pass
    return fields


def get_session_db_info(session_id):
    """Query session-store.db for turn count, summary, and checkpoints."""
    info = {"turn_count": 0, "summary": None, "checkpoint_count": 0, "checkpoints": []}
    if not SESSION_STORE_DB.exists():
        return info
    try:
        conn = sqlite3.connect(str(SESSION_STORE_DB), timeout=5)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,))
        info["turn_count"] = c.fetchone()[0]

        c.execute("SELECT summary FROM sessions WHERE id = ?", (session_id,))
        row = c.fetchone()
        if row:
            info["summary"] = row[0]

        c.execute(
            "SELECT checkpoint_number, title, overview, created_at FROM checkpoints "
            "WHERE session_id = ? ORDER BY checkpoint_number DESC LIMIT 5",
            (session_id,)
        )
        for row in c.fetchall():
            info["checkpoints"].append({
                "number": row[0],
                "title": row[1],
                "overview": (row[2] or "")[:200],
                "created_at": row[3]
            })
        info["checkpoint_count"] = len(info["checkpoints"])

        conn.close()
    except Exception:
        pass
    return info


def get_log_activity(log_file):
    """Determine activity status from log file modification time."""
    if log_file is None:
        return {"status": "unknown", "last_activity": None}
    try:
        mtime = os.path.getmtime(log_file)
        last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        age_seconds = (datetime.now(tz=timezone.utc) - datetime.fromtimestamp(mtime, tz=timezone.utc)).total_seconds()
        status = "working" if age_seconds < 30 else "idle"
        return {"status": status, "last_activity": last_activity}
    except Exception:
        return {"status": "unknown", "last_activity": None}


def gather_sessions():
    """Gather all active session data."""
    pids = get_copilot_pids()
    sessions = []

    for pid in pids:
        session_id, log_file = pid_to_session_id(pid)
        if not session_id:
            continue

        ws = read_workspace_yaml(session_id)
        db_info = get_session_db_info(session_id)
        activity = get_log_activity(log_file)
        log_tail = read_log_tail(log_file)

        current_intent = extract_current_intent(log_tail)
        token_usage = extract_token_usage(log_file)
        waiting_for_user = detect_waiting_for_user(log_tail, log_file)
        context_window = extract_context_window(log_tail)

        session_name = ws.get("name", db_info.get("summary") or "Unnamed Session")
        session_activity = extract_activity_feed(log_tail, session_name)

        status = activity["status"]
        if waiting_for_user and status == "idle":
            status = "waiting"

        sessions.append({
            "session_id": session_id,
            "pid": pid,
            "name": session_name,
            "current_intent": current_intent,
            "cwd": ws.get("cwd", "Unknown"),
            "repository": ws.get("repository", None),
            "branch": ws.get("branch", None),
            "host_type": ws.get("host_type", None),
            "created_at": ws.get("created_at", None),
            "updated_at": ws.get("updated_at", None),
            "status": status,
            "last_activity": activity["last_activity"],
            "turn_count": db_info["turn_count"],
            "checkpoint_count": db_info["checkpoint_count"],
            "checkpoints": db_info["checkpoints"],
            "token_usage": token_usage,
            "context_window": context_window,
            "activity_feed": session_activity,
            "remote_steerable": ws.get("remote_steerable", "false") == "true",
        })

    return {"sessions": sessions}


def extract_token_timeseries(log_file):
    """Extract token usage bucketed by minute from a log file."""
    if log_file is None:
        return {}
    cache_key = str(log_file)
    cached = _timeseries_cache.get(cache_key, {"offset": 0, "buckets": {}})

    try:
        file_size = os.path.getsize(log_file)
        if file_size <= cached["offset"]:
            return cached["buckets"]

        current_ts_minute = None
        pending_count = None

        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            if cached["offset"] > 0:
                f.seek(cached["offset"])
                f.readline()

            for line in f:
                if len(line) > 24 and line[4] == '-' and line[10] == 'T':
                    ts_part = line[:16]
                    if ts_part[13] == ':':
                        current_ts_minute = ts_part

                if '"token_count"' in line:
                    m = re.search(r'"token_count":\s*(\d+)', line)
                    if m:
                        pending_count = int(m.group(1))
                elif '"token_type"' in line and pending_count is not None:
                    m = re.search(r'"token_type":\s*"(input|output|cache_read|cache_write)"', line)
                    if m and current_ts_minute and pending_count > 0:
                        bucket = cached["buckets"].setdefault(current_ts_minute, {
                            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0
                        })
                        bucket[m.group(1)] += pending_count
                    pending_count = None
                else:
                    if '"token_type"' not in line and '"token_count"' not in line:
                        pending_count = None

        cached["offset"] = file_size
        _timeseries_cache[cache_key] = cached
    except Exception:
        pass
    return cached["buckets"]


def gather_token_timeseries():
    """Gather token timeseries across all active sessions, merged by minute."""
    from datetime import timedelta
    pids = get_copilot_pids()
    merged = {}

    for pid in pids:
        _, log_file = pid_to_session_id(pid)
        if not log_file:
            continue
        buckets = extract_token_timeseries(log_file)
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
    """Bring the terminal window hosting an agent PID to foreground.

    Walks up the process tree (target → cmd.exe → WindowsTerminal/conhost)
    and focuses the WindowsTerminal window if found, else the topmost ancestor
    with a visible window.
    """
    ps_script = f"""
Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class WinFocus {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lp);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    private static Dictionary<uint, IntPtr> _cache = new Dictionary<uint, IntPtr>();
    private static bool _built = false;

    public static IntPtr GetWindow(uint pid) {{
        if (!_built) {{
            EnumWindows(delegate(IntPtr h, IntPtr lp) {{
                uint wp; GetWindowThreadProcessId(h, out wp);
                if (IsWindowVisible(h) && !_cache.ContainsKey(wp)) _cache[wp] = h;
                return true;
            }}, IntPtr.Zero);
            _built = true;
        }}
        IntPtr r; return _cache.TryGetValue(pid, out r) ? r : IntPtr.Zero;
    }}

    public static bool Focus(IntPtr h) {{
        if (IsIconic(h)) ShowWindow(h, 9);
        return SetForegroundWindow(h);
    }}
}}
'@

$targetPid = {target_pid}
$visited = @{{}}
$current = $targetPid
$candidates = @()

for ($i = 0; $i -lt 10; $i++) {{
    if ($visited[$current]) {{ break }}
    $visited[$current] = $true
    $proc = Get-Process -Id $current -ErrorAction SilentlyContinue
    if (-not $proc) {{ break }}
    $hWnd = [WinFocus]::GetWindow([uint32]$current)
    if ($hWnd -ne [IntPtr]::Zero -and $proc.ProcessName -ne 'explorer' -and $proc.ProcessName -ne 'svchost') {{
        $candidates += @{{ pid=$current; name=$proc.ProcessName; hwnd=$hWnd }}
    }}
    $wmi = Get-CimInstance Win32_Process -Filter "ProcessId=$current" -ErrorAction SilentlyContinue
    if (-not $wmi) {{ break }}
    $current = $wmi.ParentProcessId
}}

$best = $null
foreach ($c in $candidates) {{
    if ($c.name -eq 'WindowsTerminal') {{ $best = $c; break }}
}}
if (-not $best -and $candidates.Count -gt 0) {{ $best = $candidates[-1] }}

if ($best) {{
    [WinFocus]::Focus($best.hwnd) | Out-Null
    Write-Output "FOCUSED:$($best.name):$($best.pid)"
}} else {{
    Write-Output "NO_WINDOW"
}}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=8
        )
        output = result.stdout.strip()
        if output.startswith("FOCUSED:"):
            return {"ok": True, "focused": output}
        return {"ok": False, "error": "No terminal window found"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
