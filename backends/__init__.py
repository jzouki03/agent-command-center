"""Agent backends for command-center.

Each backend module exposes:
- gather_sessions() -> dict
- gather_token_timeseries() -> dict
- focus_terminal_for_pid(pid) -> dict

Select via the AGENT_TYPE env var: "copilot" (default) or "claude".
"""
