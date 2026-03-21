import asyncio
import ctypes
import ctypes.wintypes as wt
import logging
import msvcrt
import os
import sys
import threading
import time
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

# ── ConPTY helpers (Windows) ────────────────────────────────────────────────

k32 = ctypes.windll.kernel32
HANDLE = wt.HANDLE
DWORD = wt.DWORD
HRESULT = ctypes.HRESULT
BOOL = wt.BOOL
LPVOID = wt.LPVOID
SIZE_T = ctypes.c_size_t

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
INVALID_HANDLE_VALUE = HANDLE(-1).value


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
        ("lpAttributeList", LPVOID),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", DWORD),
        ("lpSecurityDescriptor", LPVOID),
        ("bInheritHandle", BOOL),
    ]


def _create_pipe():
    """Create an inheritable pipe, return (read_handle, write_handle)."""
    read_h = HANDLE()
    write_h = HANDLE()
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.lpSecurityDescriptor = None
    sa.bInheritHandle = True
    if not k32.CreatePipe(ctypes.byref(read_h), ctypes.byref(write_h), ctypes.byref(sa), 0):
        raise ctypes.WinError()
    return read_h.value, write_h.value


class ConPTY:
    """Spawn a process inside a Windows ConPTY."""

    def __init__(self, cols: int = 80, rows: int = 24):
        self.pty_handle = None
        self.process_handle = None
        self.pid = 0
        self._pty_input_write = None
        self._pty_output_read = None
        self._attr_list_buf = None
        self.cols = cols
        self.rows = rows

    def spawn(self, cmdline: str):
        # Create pipes: we write to pty_input, read from pty_output
        pty_input_read, self._pty_input_write = _create_pipe()
        self._pty_output_read, pty_output_write = _create_pipe()

        # Create pseudo console (pack COORD as int to avoid struct-by-value issues)
        coord_packed = self.cols | (self.rows << 16)
        pty_h = HANDLE()
        hr = k32.CreatePseudoConsole(coord_packed, HANDLE(pty_input_read), HANDLE(pty_output_write), DWORD(0), ctypes.byref(pty_h))
        if hr != 0:
            raise ctypes.WinError(hr)
        self.pty_handle = pty_h.value

        # Close pipe ends that the ConPTY now owns
        k32.CloseHandle(HANDLE(pty_input_read))
        k32.CloseHandle(HANDLE(pty_output_write))

        # Prepare startup info with attribute list
        attr_count = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_count))
        self._attr_list_buf = (ctypes.c_byte * attr_count.value)()
        if not k32.InitializeProcThreadAttributeList(self._attr_list_buf, 1, 0, ctypes.byref(attr_count)):
            raise ctypes.WinError()
        if not k32.UpdateProcThreadAttribute(
            self._attr_list_buf, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            HANDLE(self.pty_handle), ctypes.sizeof(HANDLE), None, None,
        ):
            raise ctypes.WinError()

        si = STARTUPINFOEXW()
        ctypes.memset(ctypes.byref(si), 0, ctypes.sizeof(si))
        si.cb = ctypes.sizeof(si)
        si.lpAttributeList = ctypes.cast(self._attr_list_buf, LPVOID)

        pi = PROCESS_INFORMATION()
        if not k32.CreateProcessW(
            None, cmdline, None, None, False,
            EXTENDED_STARTUPINFO_PRESENT, None, None,
            ctypes.byref(si), ctypes.byref(pi),
        ):
            raise ctypes.WinError()

        self.process_handle = pi.hProcess
        self.pid = pi.dwProcessId
        k32.CloseHandle(pi.hThread)

        log.info("ConPTY process started (pid %d, %dx%d)", self.pid, self.cols, self.rows)

    def write(self, data: bytes):
        written = DWORD()
        k32.WriteFile(HANDLE(self._pty_input_write), data, len(data), ctypes.byref(written), None)

    def read(self, size: int = 4096) -> bytes:
        buf = (ctypes.c_ubyte * size)()
        read_count = DWORD()
        ok = k32.ReadFile(HANDLE(self._pty_output_read), buf, size, ctypes.byref(read_count), None)
        if not ok or read_count.value == 0:
            return b""
        return bytes(buf[: read_count.value])

    def resize(self, cols: int, rows: int):
        self.cols = cols
        self.rows = rows
        if self.pty_handle:
            # Pack COORD as raw int to avoid ctypes struct-by-value issues
            coord_packed = cols | (rows << 16)
            hr = k32.ResizePseudoConsole(HANDLE(self.pty_handle), coord_packed)
            if hr != 0:
                log.warning("ResizePseudoConsole failed: 0x%08x", hr)

    @property
    def returncode(self) -> int | None:
        code = DWORD()
        k32.GetExitCodeProcess(HANDLE(self.process_handle), ctypes.byref(code))
        if code.value == 259:  # STILL_ACTIVE
            return None
        return code.value

    def terminate(self):
        k32.TerminateProcess(HANDLE(self.process_handle), 1)

    def close(self):
        if self.pty_handle:
            k32.ClosePseudoConsole(HANDLE(self.pty_handle))
            self.pty_handle = None
        if self._pty_input_write:
            k32.CloseHandle(HANDLE(self._pty_input_write))
            self._pty_input_write = None
        if self._pty_output_read:
            k32.CloseHandle(HANDLE(self._pty_output_read))
            self._pty_output_read = None
        if self.process_handle:
            k32.CloseHandle(HANDLE(self.process_handle))
            self.process_handle = None
        if self._attr_list_buf:
            k32.DeleteProcThreadAttributeList(self._attr_list_buf)
            self._attr_list_buf = None


# ── SSH Session ─────────────────────────────────────────────────────────────


class SSHSession:
    def __init__(self):
        self.pty: ConPTY | None = None
        self.buffer: list[str] = []
        self.read_cursor: int = 0
        self._reader_task: asyncio.Task | None = None
        self._keyboard_task: asyncio.Task | None = None
        self._resize_task: asyncio.Task | None = None
        self._log = None

    async def connect(self, cmd: list[str]):
        if self.pty and self.pty.returncode is None:
            raise RuntimeError("Already connected. Disconnect first.")

        log.info("SSH connecting: %s", " ".join(cmd))
        self._log = open(LOG_FILE, "a", encoding="utf-8")
        self.buffer = []
        self.read_cursor = 0

        try:
            ts = os.get_terminal_size()
            cols, rows = ts.columns, ts.lines
            log.info("Terminal size detected: %dx%d", cols, rows)
        except OSError:
            cols, rows = 80, 24
            log.info("Terminal size not available, using default: %dx%d", cols, rows)

        self.pty = ConPTY(cols=cols, rows=rows)
        cmdline = " ".join(cmd)
        self.pty.spawn(cmdline)
        self._reader_thread = threading.Thread(target=self._reader_thread_func, daemon=True)
        self._reader_thread.start()
        if interactive_mode:
            self._start_keyboard_thread()
        self._resize_task = asyncio.create_task(self._resize_loop())

    def _reader_thread_func(self):
        """Blocking reader running in its own thread."""
        try:
            while True:
                chunk = self.pty.read(4096)
                if not chunk:
                    rc = self.pty.returncode
                    log.info("SSH session ended (exit code %s)", rc)
                    break
                text = chunk.decode("utf-8", errors="replace")
                self.buffer.append(text)
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
                if self._log:
                    self._log.write(text)
                    self._log.flush()
                if len(self.buffer) > 10000:
                    self.buffer = self.buffer[-5000:]
                    self.read_cursor = 0
        except OSError:
            rc = self.pty.returncode if self.pty else None
            log.info("SSH session ended (exit code %s)", rc)

    def _start_keyboard_thread(self):
        """Start keyboard reader in a dedicated daemon thread."""
        t = threading.Thread(target=self._keyboard_thread, daemon=True)
        t.start()

    def _keyboard_thread(self):
        """Blocking keyboard reader running in its own thread."""
        ext_map = {
            "H": "\033[A",  # Up
            "P": "\033[B",  # Down
            "M": "\033[C",  # Right
            "K": "\033[D",  # Left
            "G": "\033[H",  # Home
            "O": "\033[F",  # End
            "I": "\033[5~",  # PgUp
            "Q": "\033[6~",  # PgDn
            "S": "\033[3~",  # Delete
            "R": "\033[2~",  # Insert
        }
        try:
            while self.connected:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "\x00" or ch == "\xe0":
                        ch2 = msvcrt.getwch()
                        seq = ext_map.get(ch2, "")
                        if seq:
                            self.pty.write(seq.encode())
                    else:
                        self.pty.write(ch.encode())
                else:
                    time.sleep(0.02)
        except OSError:
            pass

    async def _resize_loop(self):
        """Watch for terminal resize and update ConPTY."""
        last_cols, last_rows = self.pty.cols, self.pty.rows
        await asyncio.sleep(3)  # let initial connection settle
        try:
            while self.connected:
                await asyncio.sleep(1)
                try:
                    ts = os.get_terminal_size()
                    new_cols, new_rows = ts.columns, ts.lines
                    if new_cols != last_cols or new_rows != last_rows:
                        # Debounce: confirm size is stable
                        await asyncio.sleep(0.5)
                        ts2 = os.get_terminal_size()
                        if ts2.columns == new_cols and ts2.lines == new_rows:
                            last_cols, last_rows = new_cols, new_rows
                            self.pty.resize(last_cols, last_rows)
                            log.info("Terminal resized: %dx%d", last_cols, last_rows)
                except OSError:
                    pass
        except asyncio.CancelledError:
            pass

    async def send(self, data: str):
        if not self.connected:
            raise RuntimeError("Not connected.")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.pty.write, data.encode())

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
        return self.pty is not None and self.pty.returncode is None

    async def disconnect(self):
        pid = self.pty.pid if self.pty else "N/A"
        log.info("SSH disconnecting (pid %s)", pid)
        if self.pty and self.pty.returncode is None:
            self.pty.terminate()
            for _ in range(50):
                if self.pty.returncode is not None:
                    break
                await asyncio.sleep(0.1)
            log.info("SSH process terminated (exit code %s)", self.pty.returncode)
        for attr in ("_resize_task",):
            task = getattr(self, attr, None)
            if task:
                task.cancel()
                setattr(self, attr, None)
        if self.pty:
            self.pty.close()
            self.pty = None
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
    # Poll until output stabilizes or timeout
    await asyncio.sleep(0.1)
    for _ in range(18):  # up to ~2s total
        output = session.get_new()
        if output:
            await asyncio.sleep(0.1)
            more = session.get_new()
            if not more:
                return output
            output += more
            return output
        await asyncio.sleep(0.1)
    return session.get_new() or "(no new output yet)"


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
        return "Connected (pid {})".format(session.pty.pid)
    if session.pty and session.pty.returncode is not None:
        return f"Exited with code {session.pty.returncode}"
    return "Not connected"


@mcp.tool()
async def ssh_disconnect() -> str:
    """Disconnect the current SSH session."""
    log.info("Tool ssh_disconnect()")
    if not session.connected:
        return "Not connected."
    await session.disconnect()
    return "Disconnected."


interactive_mode = False

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="mitm-ssh MCP server")
    parser.add_argument("-i", "--interactive", action="store_true", help="Enable local keyboard input to SSH session")
    args = parser.parse_args()
    interactive_mode = args.interactive

    log.info("mitm-ssh MCP server starting on http://127.0.0.1:%d/mcp", MCP_PORT)
    if interactive_mode:
        log.info("Interactive mode: keyboard input enabled")
    mcp.run(transport="streamable-http")
