# Replit snapshot

Verbatim copies of the files that run on Replit app `medical-board-history`
(replId `0910c133-b692-4c80-b4d8-39d37884f87b`, last updated 2026-04-06),
read through the Replit MCP connector during the audit session. These are the
source of truth for the rebuild. They are reference only and are not imported
by anything.

Included: `app.py`, `.replit`, `pyproject.toml`.

Not included: the original `database.py`. Its full text is reproduced inside
`/database.py` at the repo root in modified form; the modifications are listed
in HANDOFF.md section 3. The other Replit files (`replit.md`, `static/*`,
`.streamlit/config.toml`, `uv.lock`, `main.py`, the legacy
`medical_board_with_history.py`, the four guide markdown files,
`requirements_history.txt`) were read but are either generated, static assets,
or the stale legacy version that is also on GitHub.

To re-read any Replit file live: load `mcp__Replit__read_app_file` via
ToolSearch and call it with the replId above and the file path.
