"""`sch dashboard`: offline-first ANSI workspace switchboard."""

import json
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass

from .. import dashboard as dashboard_data
from .. import procs
from ..config import die
from .status import format_status


ENTER_SCREEN = "\x1b[?1049h\x1b[?25l"
LEAVE_SCREEN = "\x1b[?25h\x1b[?1049l"
CLEAR = "\x1b[H\x1b[2J"
DEFAULT_INTERVAL = 20.0
WEB_READY_TIMEOUT = 30.0
# add-pi-harness (design D11): `sch web` is opencode-only, so the web column and
# the `w` action are unavailable for every other harness. Kept as a RULE ("no web
# UI") rather than a claude special case, so the dashboard needs no change when a
# harness is added.
HARNESSES_WITHOUT_WEB = ("claude", "pi")
_URL_RE = re.compile(r"^http://127\.0\.0\.1:([1-9][0-9]{0,4})/?$")


@dataclass
class WebBridge:
    pid: int
    url: str
    process: object = None


def _clean_text(text):
    return "".join(
        char if char >= " " and char != "\x7f" else " " for char in str(text)
    )


def truncate(text, width):
    text = _clean_text(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "~"


def _columns(width):
    fixed = (12, 10, 8, 7, 8, 20, 8, 5)
    if width >= 88:
        return fixed
    available = max(8, width - 7)
    weights = (3, 2, 2, 1, 2, 3, 2, 1)
    total = sum(weights)
    return tuple(max(1, available * weight // total) for weight in weights)


def _row(values, widths):
    return " ".join(truncate(value, width).ljust(width) for value, width in zip(values, widths))


def format_age(seconds):
    seconds = int(seconds)
    if seconds < 120:
        return "{}s".format(seconds)
    minutes = seconds // 60
    if minutes < 120:
        return "{}m".format(minutes)
    hours = minutes // 60
    if hours < 48:
        return "{}h".format(hours)
    return "{}d".format(hours // 24)


def live_cell(manifest_age_s):
    """LIVE column: passive microVM liveness from L2 manifest freshness.

    ``● <age>`` fresh manifest (microVM alive within the last checkpoint
    intervals), ``- <age>`` stale manifest (microVM gone), ``?`` manifest
    unreadable/absent (never checkpointed, or S3 read failed).
    """
    if manifest_age_s is None:
        return "?"
    marker = "\u25cf" if manifest_age_s <= dashboard_data.LIVE_THRESHOLD_S else "-"
    return "{} {}".format(marker, format_age(manifest_age_s))


def status_lines(row):
    try:
        data = json.loads(row.status_json)
    except (TypeError, ValueError):
        data = {"state": "unknown"}
    if not isinstance(data, dict):
        data = {"state": "unknown"}
    data = {
        key: _clean_text(value) if isinstance(value, str) else value
        for key, value in data.items()
    }
    lines = ["status {}".format(row.name)]
    lines.extend(format_status(data).splitlines())
    if row.error:
        lines.append("read error: {}".format(row.error))
    return lines


def render_lines(snapshot, selected_name, bridges, width, height, detail=False,
                 message="", now=time.time):
    """Pure renderer: return terminal-sized, ANSI-free frame lines."""
    widths = _columns(width)
    refreshed_at = snapshot.refreshed_at
    age = max(0, int(now() - refreshed_at)) if refreshed_at else 0
    lines = [truncate("SCH dashboard | offline snapshot {}s old".format(age), width)]
    lines.append(_row(("WORKSPACE", "HARNESS", "STORAGE", "LIVE", "TASK", "HEARTBEAT", "CKPT", "WEB"), widths)[:width])
    selected = None
    max_rows = max(0, height - 6)
    names = [row.name for row in snapshot.workspaces]
    start = 0
    if selected_name in names and max_rows:
        selected_index = names.index(selected_name)
        start = max(0, min(selected_index - max_rows // 2, len(names) - max_rows))
    for row in snapshot.workspaces[start : start + max_rows]:
        if row.name == selected_name:
            selected = row
        web = (
            "n/a" if row.harness in HARNESSES_WITHOUT_WEB
            else ("\u25cf" if bridge_alive(bridges.get(row.name)) else "-")
        )
        marker = ">" if row.name == selected_name else " "
        values = (row.name, row.harness, row.storage, live_cell(row.manifest_age_s),
                  row.task_state, row.heartbeat or "-", row.checkpoint or "-", web)
        lines.append(truncate(marker + _row(values, widths), width))
    if selected is None and snapshot.workspaces:
        selected = snapshot.workspaces[0]
    if selected is not None:
        details = status_lines(selected) if detail else [
            "selected {} | harness={} storage={} task={} live={}{}".format(
                selected.name, selected.harness, selected.storage,
                selected.task_state, live_cell(selected.manifest_age_s),
                " | {}".format(selected.error) if selected.error else "",
            )
        ]
        lines.extend(truncate(line, width) for line in details)
    elif not snapshot.workspaces:
        lines.append("No workspaces in the local index/registry.")
    if message:
        lines.append(truncate("message: " + message, width))
    web_key = (
        "w web"
        if selected is None or selected.harness not in HARNESSES_WITHOUT_WEB
        else "w web(n/a)"
    )
    footer = "up/down move | Enter run | S shell | {} | s status | r refresh | d delete | q quit".format(web_key)
    lines.append(truncate(footer, width))
    if len(lines) > height:
        lines = lines[: max(0, height - 1)] + [truncate(footer, width)]
    return lines


def render_frame(*args, **kwargs):
    return CLEAR + "\r\n".join(render_lines(*args, **kwargs))


def decode_input(value, platform="posix"):
    """Normalize one raw read into a dashboard action."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if value in ("\x1b[A", "\xe0H", "\x00H"):
        return "up"
    if value in ("\x1b[B", "\xe0P", "\x00P"):
        return "down"
    if value in ("\r", "\n"):
        return "run"
    if value == "S":
        return "shell"
    if value in ("w", "s", "r", "q", "d"):
        return value
    return None


class Terminal:
    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.saved = None
        self.windows_output = None

    def enable_vt(self):
        if os.name != "nt":
            return True
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                return False
            self.windows_output = (handle, mode.value)
            return True
        except (AttributeError, OSError):
            return False

    def enter(self):
        if os.name == "posix":
            import termios
            import tty

            self.saved = termios.tcgetattr(self.stdin.fileno())
            tty.setraw(self.stdin.fileno())
        elif self.windows_output is not None:
            import ctypes

            handle, mode = self.windows_output
            if not ctypes.windll.kernel32.SetConsoleMode(handle, mode | 0x0004):
                raise OSError("cannot enable Windows VT output")
        try:
            self.stdout.write(ENTER_SCREEN)
            self.stdout.flush()
        except BaseException:
            self._restore_input()
            raise

    def leave(self):
        try:
            self.stdout.write(LEAVE_SCREEN)
            self.stdout.flush()
        finally:
            self._restore_input()

    def _restore_input(self):
        if self.saved is not None:
            import termios

            termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, self.saved)
            self.saved = None
        if self.windows_output is not None:
            import ctypes

            handle, mode = self.windows_output
            ctypes.windll.kernel32.SetConsoleMode(handle, mode)

    def read_action(self, timeout=0.1):
        if os.name == "nt":
            import msvcrt

            end = time.time() + timeout
            while time.time() < end and not msvcrt.kbhit():
                time.sleep(0.01)
            if not msvcrt.kbhit():
                return None
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                char += msvcrt.getwch()
            return decode_input(char, "nt")
        ready, _, _ = select.select([self.stdin], [], [], timeout)
        if not ready:
            return None
        first = os.read(self.stdin.fileno(), 1)
        if first == b"\x1b":
            ready, _, _ = select.select([self.stdin], [], [], 0.01)
            if ready:
                first += os.read(self.stdin.fileno(), 2)
        return decode_input(first)


def cli_argv(command, workspace_name):
    entrypoint = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "__main__.py"))
    return [sys.executable, entrypoint, command, workspace_name]


def bridge_alive(bridge):
    if bridge is None:
        return False
    if bridge.process is not None:
        return bridge.process.poll() is None
    try:
        os.kill(bridge.pid, 0)
        return True
    except OSError:
        return False


def _detached_options(platform=None):
    platform = os.name if platform is None else platform
    if platform == "nt":
        return {"creationflags": 0x00000008 | 0x00000200}
    return {"start_new_session": True}


def _read_ready_line(child, timeout):
    ready = queue.Queue(maxsize=1)

    def read():
        try:
            ready.put(child.stdout.readline() if child.stdout else "")
        except Exception as exc:
            ready.put(exc)

    threading.Thread(target=read, name="sch-dashboard-web-ready", daemon=True).start()
    try:
        result = ready.get(timeout=timeout)
    except queue.Empty:
        raise RuntimeError("timed out waiting for localhost URL")
    if isinstance(result, Exception):
        raise result
    return result.strip()


def start_web_bridge(row, bridges, popen=subprocess.Popen, browser_open=webbrowser.open,
                     ready_timeout=WEB_READY_TIMEOUT):
    if row.harness in HARNESSES_WITHOUT_WEB:
        return "web is unavailable for {} workspaces".format(row.harness)
    current = bridges.get(row.name)
    if bridge_alive(current):
        try:
            browser_open(current.url)
        except Exception as exc:
            return "bridge ready at {} (browser: {})".format(current.url, exc)
        return "reopened {}".format(current.url)
    bridges.pop(row.name, None)
    argv = cli_argv("web", row.name) + ["--no-browser"]
    try:
        child = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            **_detached_options()
        )
        line = _read_ready_line(child, ready_timeout)
    except (OSError, RuntimeError) as exc:
        if "child" in locals() and child.poll() is None:
            child.terminate()
        return "cannot start web bridge: {}".format(exc)
    match = _URL_RE.fullmatch(line)
    if not match or int(match.group(1)) > 65535:
        if child.poll() is None:
            child.terminate()
        return "web bridge did not report a valid localhost URL"
    bridge = WebBridge(child.pid, line, child)
    bridges[row.name] = bridge
    try:
        browser_open(line)
    except Exception as exc:
        return "bridge ready at {} (browser: {})".format(line, exc)
    return "web bridge ready at {}".format(line)


def move_selection(snapshot, selected_name, delta):
    names = [row.name for row in snapshot.workspaces]
    if not names:
        return None
    if selected_name not in names:
        return names[0]
    return names[(names.index(selected_name) + delta) % len(names)]


def _parse_args(args):
    interval = DEFAULT_INTERVAL
    if not args:
        return interval
    if len(args) != 2 or args[0] != "--interval":
        die("usage: sch dashboard [--interval <seconds>]")
    try:
        interval = float(args[1])
    except ValueError:
        die("invalid --interval value '{}' (must be positive seconds)".format(args[1]))
    if interval <= 0:
        die("invalid --interval value '{}' (must be positive seconds)".format(args[1]))
    return interval


def cmd_dashboard(cfg, args):
    interval = _parse_args(args)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        die("dashboard requires an interactive terminal")
    terminal = Terminal()
    if not terminal.enable_vt():
        die("dashboard requires VT support; use Windows Terminal or another VT-compatible terminal")
    updates = queue.Queue()

    worker = dashboard_data.RefreshController(
        cfg, interval, updates
    )
    snapshot = dashboard_data.DashboardSnapshot((), 0)
    selected_name = None
    bridges = {}
    detail = False
    message = "loading offline status..."
    worker.start()
    entered = False
    try:
        terminal.enter()
        entered = True
        running = True
        while running:
            try:
                while True:
                    update = updates.get_nowait()
                    if isinstance(update, dashboard_data.DashboardSnapshot):
                        snapshot = update
                        selected_name = move_selection(snapshot, selected_name, 0)
                        # Preserve delete confirmation until next user action,
                        # otherwise the async refresh would clear it before it is
                        # ever rendered (same race as run/shell errors).
                        if not (message.startswith("deleted ") or message.startswith("delete ")):
                            message = ""
                    else:
                        message = "refresh failed: {}".format(update)
            except queue.Empty:
                pass
            size = shutil.get_terminal_size((100, 30))
            sys.stdout.write(render_frame(
                snapshot, selected_name, bridges, size.columns, size.lines,
                detail=detail, message=message,
            ))
            sys.stdout.flush()
            action = terminal.read_action()
            if action == "q":
                running = False
            elif action in ("up", "down"):
                selected_name = move_selection(snapshot, selected_name, -1 if action == "up" else 1)
            elif action == "r":
                worker.trigger()
                message = "refresh requested"
            elif action == "s":
                detail = not detail
            elif action in ("run", "shell") and selected_name:
                terminal.leave()
                entered = False
                try:
                    rc = procs.foreground_handoff(cli_argv(action, selected_name))
                    message = "{} exited with status {}".format(action, rc) if rc else ""
                except BaseException as exc:
                    message = "{} failed: {}".format(action, exc)
                finally:
                    terminal.enter()
                    entered = True
                    worker.trigger()
            elif action == "w" and selected_name:
                row = next(item for item in snapshot.workspaces if item.name == selected_name)
                message = start_web_bridge(row, bridges)
            elif action == "d" and selected_name:
                terminal.leave()
                entered = False
                try:
                    rc = procs.foreground_handoff(cli_argv("delete", selected_name))
                    if rc == 0:
                        bridges.pop(selected_name, None)
                        message = "deleted {}".format(selected_name)
                    elif rc:
                        message = "delete exited with status {}".format(rc)
                    else:
                        message = ""
                except BaseException as exc:
                    message = "delete failed: {}".format(exc)
                finally:
                    terminal.enter()
                    entered = True
                    worker.trigger()
        return 0
    finally:
        worker.stop(1)
        if entered:
            terminal.leave()
