# mitm-ssh

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows)
![MCP](https://img.shields.io/badge/MCP-streamable--http-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

Tired of Claude running SSH commands behind your back? mitm-ssh sits between Claude and the SSH session so you can watch everything as it happens — just like a real terminal.

The server runs as a separate process in a separate terminal window. You start it, keep it visible, and watch the full SSH session unfold in real time while Claude works.

An MCP server that wraps `ssh.exe` inside a Windows ConPTY, pipes its stdio through MCP tools, and displays the live terminal output in its own terminal window — like looking over Claude's shoulder. It also helps debug SSH and MCP connection issues by showing connection build-up and tear-down as status messages.

## How it works

```
Claude Code  <--MCP/HTTP-->  mitm-ssh (server.py)  <--stdin/stdout-->  ssh.exe  <-->  remote host
```

You run the server in a terminal. Claude connects over HTTP and controls the SSH session via MCP tools. You see everything happening in real time — SSH output streams to your terminal, and connect/disconnect events are logged in grey.

## Setup

Designed for Windows (uses `ssh.exe`). Requires Python 3.11+.

```bash
# 1. Start the server (keep this terminal open)
uv run server.py
# or without uv: pip install mcp && python server.py

# 1b. Optional: enable local keyboard input to the SSH session
uv run server.py -i

# 2. Register with Claude Code (once)
claude mcp add -s user -t http mitm-ssh http://127.0.0.1:8022/mcp
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `ssh_connect` | Spawn ssh.exe to a host (with optional user, port, identity file) |
| `ssh_run` | Send a command + Enter, return output |
| `ssh_send_raw` | Send raw text without newline (for prompts, passwords) |
| `ssh_read` | Read new output since last read |
| `ssh_read_all` | Read recent output from the full buffer |
| `ssh_status` | Check if an SSH session is active |
| `ssh_disconnect` | Terminate the SSH session |

## Usage

With the server running, just ask Claude:

> Connect to myserver and check disk usage

Claude will call `ssh_connect`, then `ssh_run` with `df -h`, and report back.

## Notes

- **Key-based auth recommended** — password auth is unreliable without a real PTY
- **ANSI pass-through** — ConPTY gives ssh.exe a real terminal, so you get colors and full TUI output
- **Ctrl+C** is forwarded to the SSH session (transparent) — use **Ctrl+Break** to kill the server
- SSH output is also appended to `ssh_output.log`
- Server listens on `127.0.0.1:8022` (localhost only)
