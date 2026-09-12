"""Process helpers: interactive exec/wait, foreground spawn with inherited
stdio, and temp-file plumbing for `aws bedrock-agentcore invoke-agent-runtime`
output files.
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


def _save_terminal_state():
    if os.name == "posix":
        import termios

        if not sys.stdin.isatty():
            return None
        return ("posix", sys.stdin.fileno(), termios.tcgetattr(sys.stdin.fileno()))

    import ctypes

    kernel32 = ctypes.windll.kernel32
    modes = []
    for stream_id in (-10, -11):  # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE
        handle = kernel32.GetStdHandle(stream_id)
        mode = ctypes.c_ulong()
        if handle not in (0, -1) and kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            modes.append((handle, mode.value))
    return ("windows", modes)


def _restore_terminal_state(state):
    if state is None:
        return
    if state[0] == "posix":
        import termios

        termios.tcsetattr(state[1], termios.TCSADRAIN, state[2])
        return

    import ctypes

    kernel32 = ctypes.windll.kernel32
    for handle, mode in state[1]:
        kernel32.SetConsoleMode(handle, mode)


# Mirror of tunnel/attach.js restoreTerminal(): disable mouse tracking
# (1000/1002/1003), focus events (1004), SGR/urxvt mouse encodings
# (1006/1015) and bracketed paste (2004) that a remote TUI (e.g. opencode)
# may have enabled through the interactive tunnel, then re-show the cursor.
# These modes live in the *local terminal emulator*, not in the tty driver,
# so a termios restore alone cannot undo them; without this reset a Ctrl+]
# detach leaves the shell spewing SGR mouse reports ("35;92;46M...") on
# every pointer movement.
TERMINAL_MODE_RESET = (
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1004l"
    "\x1b[?1006l\x1b[?1015l\x1b[?2004l\x1b[?25h"
)


def reset_terminal_modes():
    """Best-effort reset of emulator modes left enabled by a remote TUI."""
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write(TERMINAL_MODE_RESET)
        sys.stdout.flush()
    except OSError:
        pass


def foreground_handoff(argv):
    """Run ``argv`` with inherited stdio and restore console state afterward."""
    state = _save_terminal_state()
    try:
        resolved = argv[0]
        if os.name != "posix":
            resolved = shutil.which(resolved) or resolved
        return subprocess.call([resolved] + list(argv[1:]))
    finally:
        if sys.exc_info()[0] is None:
            _restore_terminal_state(state)
        else:
            try:
                _restore_terminal_state(state)
            except Exception:
                pass


def exec_or_wait(argv, presence=None):
    """Hand the terminal to ``argv`` (the interactive CommandShell client)
    and never return normally: exits with the child's return code.

    Historically the POSIX path replaced this process (``os.execvp``,
    matching the bash reference's ``exec "$@"``), but that made post-exit
    cleanup impossible: `agentcore exec --it` restores raw mode on detach
    yet never disables the emulator-level modes (mouse tracking, bracketed
    paste) that a remote TUI enabled through the tunnel, so a Ctrl+] detach
    left the local shell flooded with SGR mouse reports. Running the client
    as a foreground child lets us restore termios state and emit
    ``TERMINAL_MODE_RESET`` once it exits, on both platforms.

    Windows note: ``subprocess.call`` does not consult PATHEXT the way a
    shell does, so npm-installed .cmd/.bat shims (e.g. `agentcore`) are
    invisible to it even though shutil.which() finds them — hence the
    explicit resolution.
    """
    resolved = argv[0]
    if os.name != "posix":
        resolved = shutil.which(resolved) or resolved
    try:
        state = _save_terminal_state()
    except Exception:
        state = None
    try:
        if presence is not None:
            presence.start()
        rc = subprocess.call([resolved] + list(argv[1:]))
    except FileNotFoundError:
        print("sch: '{}' not found".format(argv[0]), file=sys.stderr)
        raise SystemExit(1)
    except OSError as exc:
        print("sch: cannot exec '{}': {}".format(argv[0], exc), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        # subprocess.call already reaped the child on interrupt; exit with
        # the conventional SIGINT status after restoring the terminal.
        rc = 130
    finally:
        if presence is not None:
            presence.close()
        try:
            _restore_terminal_state(state)
        except Exception:
            pass
        reset_terminal_modes()
    raise SystemExit(rc)


def require_command(command, install_hint):
    """Exit before remote work when a required executable is unavailable."""
    if shutil.which(command):
        return
    print(
        "sch: '{}' not found on PATH; {}".format(command, install_hint),
        file=sys.stderr,
    )
    raise SystemExit(1)


def run_foreground(argv, env=None):
    """Run ``argv`` as a foreground child with stdio fully inherited (no
    pipes): used for ``acp``/``attach``'s Node bridge processes, so the
    editor's JSON-RPC frames pass through untouched and this process can
    still run cleanup code after the child exits.

    Returns the child's exit code.
    """
    try:
        return subprocess.call(argv, env=env)
    except FileNotFoundError:
        print("sch: '{}' not found".format(argv[0]), file=sys.stderr)
        return 1


class SyncStartError(RuntimeError):
    """The sync helper failed before it declared a safe ready barrier."""


def supervise_interactive(sync_argv, exec_argv=None, after_ready=None, presence=None):
    """Run a watch-mode helper beside CommandShell.

    The helper's stdout is a JSON control protocol; its stderr deliberately
    remains inherited for progress and warning diagnostics. The remote shell
    is started only after a barrier-backed ``ready`` record.
    """
    try:
        helper = subprocess.Popen(
            sync_argv,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise SyncStartError("cannot start sync helper: {}".format(exc)) from exc

    child = None
    terminal_state = None
    try:
        line = helper.stdout.readline() if helper.stdout else ""
        if not line:
            raise SyncStartError("sync helper exited before ready")
        try:
            control = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SyncStartError("sync helper returned invalid control output") from exc
        if control.get("type") != "ready":
            raise SyncStartError(control.get("message") or "sync helper failed before ready")
        if after_ready is not None:
            after_ready()
        if exec_argv is None:
            return 0
        if presence is not None:
            presence.start()
        try:
            terminal_state = _save_terminal_state()
        except Exception:
            terminal_state = None
        try:
            child = subprocess.Popen(exec_argv)
        except OSError as exc:
            print("sch: cannot start interactive client: {}".format(exc), file=sys.stderr)
            return 1
        while True:
            child_code = child.poll()
            if child_code is not None:
                return child_code
            helper_code = helper.poll()
            if helper_code is not None:
                print(
                    "sch: sync helper exited unexpectedly (status {}); closing the local client".format(
                        helper_code
                    ),
                    file=sys.stderr,
                )
                # The command-shell client owns Ctrl+] detach semantics. Only
                # terminate it for an actual sync helper failure; normal
                # client exit leaves the remote TUI alone.
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                return 1
            time.sleep(0.05)
    except KeyboardInterrupt:
        # Ctrl+C is also delivered to the foreground CommandShell child. Do
        # not turn this into a forced remote shutdown; just release fs state.
        return 130
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if presence is not None:
            presence.close()
        if child is not None:
            # The interactive client owned the terminal: undo any leftover
            # emulator modes (mouse tracking etc.) exactly as exec_or_wait
            # does on the standalone path.
            try:
                _restore_terminal_state(terminal_state)
            except Exception:
                pass
            reset_terminal_modes()
        if helper.poll() is None:
            helper.terminate()
            try:
                helper.wait(timeout=10)
            except subprocess.TimeoutExpired:
                helper.kill()
                helper.wait()
        # The helper writes its final barrier summary during SIGTERM handling.
        # Keep the pipe open until it exits so that write cannot raise EPIPE.
        if helper.stdout is not None:
            helper.stdout.close()


def _temp_dir():
    return os.environ.get("TMPDIR") or tempfile.gettempdir()


def temp_json_path(op):
    """Path for a scratch output file: ``<tmpdir>/sch-<op>-<pid>.json``,
    mirroring the bash reference's ``${TMPDIR:-/tmp}/sch-<op>-$$.json``.
    """
    return os.path.join(_temp_dir(), "sch-{}-{}.json".format(op, os.getpid()))


@contextlib.contextmanager
def temp_json_file(op):
    """Context manager yielding a scratch output file path, removed on
    exit regardless of outcome.
    """
    fd, path = tempfile.mkstemp(
        prefix="sch-{}-{}-".format(op, os.getpid()), suffix=".json", dir=_temp_dir()
    )
    os.close(fd)
    try:
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def null_output_path():
    """A path suitable as a throwaway AWS CLI output-file argument.

    POSIX has ``/dev/null``; Windows has no equivalent that every tool
    handles consistently, so a throwaway temp file is used there instead
    (same trick as ``bin/sch.ps1``'s ``$env:NULL_DEVICE``). Callers on
    Windows are responsible for deleting the returned path afterwards.
    """
    if os.name == "posix":
        return os.devnull
    fd, path = tempfile.mkstemp(prefix="sch-null-")
    os.close(fd)
    return path
