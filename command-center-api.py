"""
Agent Command Center — API Server

Serves a real-time monitoring dashboard for AI coding agent sessions.

Backends:
  AGENT_TYPE=copilot  (default) — GitHub Copilot CLI sessions
  AGENT_TYPE=claude            — Claude Code sessions

The HTTP/JSON interface and the dashboard HTML are unchanged across backends —
each backend produces the same session shape so the frontend stays one codebase.

Usage:
  python command-center-api.py
  AGENT_TYPE=claude python command-center-api.py     (bash/PowerShell)
  set AGENT_TYPE=claude && python command-center-api.py  (cmd.exe)

No external dependencies — uses Python stdlib only.
"""

import io
import json
import os
import signal
import subprocess
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = 9876

# ---- Backend selection -------------------------------------------------------
AGENT_TYPE = os.environ.get("AGENT_TYPE", "copilot").lower()

if AGENT_TYPE == "claude":
    from backends import claude as backend
elif AGENT_TYPE == "copilot":
    from backends import copilot as backend
else:
    print(f"ERROR: Unknown AGENT_TYPE '{AGENT_TYPE}'. Use 'copilot' or 'claude'.", file=sys.stderr)
    sys.exit(2)


class CommandCenterHandler(SimpleHTTPRequestHandler):
    """HTTP handler for the command center API.

    All endpoints delegate to the active backend. Backends produce data in a
    common shape so the frontend HTML/JS is agent-agnostic.
    """

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/sessions":
            self.send_json_response(backend.gather_sessions())
        elif parsed.path == "/api/token-timeseries":
            self.send_json_response(backend.gather_token_timeseries())
        elif parsed.path == "/api/agent-info":
            self.send_json_response({"agent_type": AGENT_TYPE})
        elif parsed.path == "/api/open-folder":
            params = parse_qs(parsed.query)
            folder = params.get("path", [None])[0]
            if folder:
                subprocess.Popen(["explorer", folder])
                self.send_json_response({"ok": True})
            else:
                self.send_json_response({"ok": False, "error": "No path"})
        elif parsed.path == "/" or parsed.path == "/index.html":
            self.serve_dashboard()
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len)) if content_len else {}

        if parsed.path == "/api/kill-session":
            pid = body.get("pid")
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    self.send_json_response({"ok": True, "killed": pid})
                except Exception as e:
                    self.send_json_response({"ok": False, "error": str(e)})
            else:
                self.send_json_response({"ok": False, "error": "No PID"})
        elif parsed.path == "/api/focus-terminal":
            pid = body.get("pid")
            if pid:
                self.send_json_response(backend.focus_terminal_for_pid(pid))
            else:
                self.send_json_response({"ok": False, "error": "No PID"})
        else:
            self.send_error(404)

    def send_json_response(self, data):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def serve_dashboard(self):
        dashboard_path = Path(__file__).parent / "command-center.html"
        if dashboard_path.exists():
            content = dashboard_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404, "Dashboard HTML not found")

    def log_message(self, format, *args):
        pass


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    print(f"Agent Command Center starting on http://localhost:{PORT}")
    print(f"   Backend:   {AGENT_TYPE}")
    print(f"   Dashboard: http://localhost:{PORT}/")
    print(f"   API:       http://localhost:{PORT}/api/sessions")
    print(f"   Press Ctrl+C to stop.\n")

    server = HTTPServer(("127.0.0.1", PORT), CommandCenterHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCommand Center stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
