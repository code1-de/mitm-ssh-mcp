# SSH MITM MCP Server

MCP server that wraps `ssh.exe` as a subprocess, pipes its stdio, displays output locally, and exposes control via MCP tools for Claude.

## Running

- Start server: `uv run server.py`
- Run Python scripts with `uv run` (not `python` directly)
- Format code: `ruff format --line-length 120`
- Lint: `ruff check`
- Working directory is already `I:\git\mitm-ssh-mcp` — do not `cd` before commands

## Architecture

- `server.py` — single-file MCP server using `FastMCP` (streamable-http transport on port 8022)
- Start server in a terminal first: `uv run server.py` — debug output visible there
- Spawns `ssh.exe` as a child process with piped stdin/stdout/stderr
- Output is buffered in memory, displayed in server terminal, and logged to `ssh_output.log`
- Key-based auth recommended (password auth unreliable without PTY)

## MCP Tools

- `ssh_connect` — spawn ssh.exe to a host
- `ssh_run` — send command + Enter, return output
- `ssh_send_raw` — send raw text (prompts, passwords)
- `ssh_read` / `ssh_read_all` — read new or full output buffer
- `ssh_status` — check connection state
- `ssh_disconnect` — terminate session

## Claude Code MCP Config

1. Start the server: `uv run server.py` (runs on http://127.0.0.1:8022/mcp)
2. Register with Claude Code: `claude mcp add -s user -t http mitm-ssh http://127.0.0.1:8022/mcp`
