import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from mcp.server.fastmcp import FastMCP

GREY = "\033[38;5;248m"
RESET = "\033[0m"
_fmt = logging.Formatter(f"{GREY}%(asctime)s [%(levelname)s] %(name)s: %(message)s{RESET}")
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(_fmt)

log = logging.getLogger("mitm-ssh")
log.setLevel(logging.DEBUG)

# Only show our own mitm-ssh log + suppress noisy loggers
logging.root.setLevel(logging.WARNING)
logging.root.handlers = [_handler]
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False


@asynccontextmanager
async def lifespan(server: FastMCP):
    log.info("MCP client connected")
    yield
    log.info("MCP client disconnected")


MCP_PORT = 8022

mcp = FastMCP("mitm-ssh", host="127.0.0.1", port=MCP_PORT, lifespan=lifespan, log_level="WARNING")

LOG_FILE = Path(__file__).parent / "ssh_output.log"


class SSHSession:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.buffer: list[str] = []
        self.read_cursor: int = 0
        self._tasks: list[asyncio.Task] = []
        self._log = None

    async def connect(self, cmd: list[str]):
        if self.process and self.process.returncode is None:
            raise RuntimeError("Already connected. Disconnect first.")

        log.info("SSH connecting: %s", " ".join(cmd))
        self._log = open(LOG_FILE, "a", encoding="utf-8")
        self.buffer = []
        self.read_cursor = 0

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info("SSH process started (pid %d)", self.process.pid)
        self._tasks = [
            asyncio.create_task(self._reader(self.process.stdout, "stdout")),
            asyncio.create_task(self._reader(self.process.stderr, "stderr")),
        ]

    async def _reader(self, stream: asyncio.StreamReader, label: str):
        log.debug("Reader started for %s", label)
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    if label == "stdout" and self.process:
                        code = await self.process.wait()
                        log.info("SSH session ended (exit code %d)", code)
                    break
                text = chunk.decode("utf-8", errors="replace")
                self.buffer.append(text)
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
                # persistent log file (tail -f ssh_output.log)
                if self._log:
                    self._log.write(text)
                    self._log.flush()
                # cap buffer
                if len(self.buffer) > 10000:
                    self.buffer = self.buffer[-5000:]
                    self.read_cursor = 0
        except asyncio.CancelledError:
            pass

    async def send(self, data: str):
        if not self.process or self.process.returncode is not None:
            raise RuntimeError("Not connected.")
        self.process.stdin.write(data.encode())
        await self.process.stdin.drain()

    def get_new(self) -> str:
        """Return output since last read, advance cursor."""
        text = "".join(self.buffer[self.read_cursor :])
        self.read_cursor = len(self.buffer)
        return text

    def get_recent(self, max_chars: int = 8000) -> str:
        """Return last max_chars of all output."""
        full = "".join(self.buffer)
        return full[-max_chars:] if len(full) > max_chars else full

    @property
    def connected(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def disconnect(self):
        log.info("SSH disconnecting (pid %s)", self.process.pid if self.process else "N/A")
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
                log.info("SSH process terminated (exit code %d)", self.process.returncode)
            except asyncio.TimeoutError:
                self.process.kill()
                log.warning("SSH process killed after timeout")
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        self.process = None
        if self._log:
            self._log.close()
            self._log = None


session = SSHSession()


@mcp.tool()
async def ssh_connect(
    host: str,
    user: str = "",
    port: int = 22,
    identity_file: str = "",
    extra_args: str = "",
) -> str:
    """Connect to an SSH host via ssh.exe.

    Args:
        host: Hostname or IP to connect to.
        user: SSH username (optional).
        port: SSH port (default 22).
        identity_file: Path to private key file (optional).
        extra_args: Additional ssh arguments as a single string (optional).
    """
    log.info("Tool ssh_connect(host=%s, user=%s, port=%d)", host, user, port)
    cmd = ["ssh.exe", "-tt", "-o", "StrictHostKeyChecking=accept-new"]
    if user:
        cmd.extend(["-l", user])
    if port != 22:
        cmd.extend(["-p", str(port)])
    if identity_file:
        cmd.extend(["-i", identity_file])
    if extra_args:
        cmd.extend(extra_args.split())
    cmd.append(host)

    await session.connect(cmd)
    # give ssh a moment to connect and produce initial output
    await asyncio.sleep(2)
    output = session.get_new()
    return f"SSH started: {' '.join(cmd)}\n\nInitial output:\n{output or '(no output yet — may still be connecting)'}"


@mcp.tool()
async def ssh_run(command: str) -> str:
    """Send a command to the SSH session and return the output.

    Args:
        command: The command to execute (Enter is appended automatically).
    """
    log.debug("Tool ssh_run(command=%r)", command)
    await session.send(command + "\n")
    await asyncio.sleep(1)
    output = session.get_new()
    return output or "(no new output yet)"


@mcp.tool()
async def ssh_send_raw(text: str) -> str:
    """Send raw text to the SSH session without appending a newline.
    Useful for answering prompts (yes/no, passwords, etc.).

    Args:
        text: Exact text to send (no newline added).
    """
    log.debug("Tool ssh_send_raw(text=%r)", text)
    await session.send(text)
    await asyncio.sleep(0.5)
    output = session.get_new()
    return output or "(no new output)"


@mcp.tool()
async def ssh_read() -> str:
    """Read new output from the SSH session since last read."""
    log.debug("Tool ssh_read()")
    if not session.connected:
        return "Not connected."
    return session.get_new() or "(no new output)"


@mcp.tool()
async def ssh_read_all(max_chars: int = 8000) -> str:
    """Read recent output from the SSH session (full buffer, not just new).

    Args:
        max_chars: Maximum characters to return (default 8000).
    """
    log.debug("Tool ssh_read_all(max_chars=%d)", max_chars)
    return session.get_recent(max_chars) or "(no output)"


@mcp.tool()
async def ssh_status() -> str:
    """Check whether an SSH session is active."""
    log.debug("Tool ssh_status()")
    if session.connected:
        return "Connected (pid {})".format(session.process.pid)
    if session.process and session.process.returncode is not None:
        return f"Exited with code {session.process.returncode}"
    return "Not connected"


@mcp.tool()
async def ssh_disconnect() -> str:
    """Disconnect the current SSH session."""
    log.info("Tool ssh_disconnect()")
    if not session.connected:
        return "Not connected."
    await session.disconnect()
    return "Disconnected."


if __name__ == "__main__":
    log.info("mitm-ssh MCP server starting on http://127.0.0.1:%d/mcp", MCP_PORT)
    mcp.run(transport="streamable-http")
