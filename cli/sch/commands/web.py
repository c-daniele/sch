"""`sch web`: browser access to the remote OpenCode web server."""

import json
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

from .. import deps, harness as harness_mod
from .. import repo, runtime, workspace
from ..config import die, runtime_arn

_SERVE_ENSURE_ATTEMPTS = 5
_SERVE_ENSURE_RETRY_DELAY_S = 2


def _parse_args(args):
    usage_msg = (
        "usage: sch web <workspace> [--harness opencode] "
        "[--storage <s3|session>] [--no-browser]"
    )
    if not args or not args[0]:
        die(usage_msg)
    ws = args[0]
    workspace.validate_workspace_name(ws)
    harness_flag = ""
    storage_flag = ""
    no_browser = False
    i = 1
    while i < len(args):
        arg = args[i]
        if arg == "--no-browser":
            no_browser = True
            i += 1
            continue
        if arg == "--harness":
            if i + 1 >= len(args):
                die("usage: --harness <opencode|claude|pi>")
            harness_flag = args[i + 1]
            i += 2
            continue
        if arg == "--storage":
            if i + 1 >= len(args):
                die("usage: --storage <s3|session>")
            storage_flag = args[i + 1]
            if storage_flag not in ("s3", "session"):
                die("--storage must be one of: s3, session")
            i += 2
            continue
        if arg.startswith("-"):
            die("unknown option '{}'".format(arg))
        die("unexpected positional argument '{}'".format(arg))
    return ws, harness_flag, storage_flag, no_browser


def _require_local_bridge():
    node_bin = deps.which_node()
    if not node_bin:
        die("'node' is required for 'sch web' (see tunnel/package.json); install Node >= 18")
    tunnel_dir = repo.tunnel_dir()
    if tunnel_dir is None:
        die(repo.missing_message("web"))
    if not (tunnel_dir / "web.js").is_file():
        die("cannot find {}/web.js (repo layout unexpected)".format(tunnel_dir))
    if not (tunnel_dir / "node_modules").is_dir():
        die(
            "tunnel/ dependencies not installed — run: (cd '{}' && npm install)".format(
                tunnel_dir
            )
        )
    return node_bin


def _stop_bridge(child):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def _forward_stderr(stream):
    for line in stream:
        sys.stderr.write(line)


def _with_basic_auth(url, user, password):
    """``http://user:password@host:port`` form of a local bridge URL (RFC 3986
    userinfo, percent-encoded). Unchanged when there is no password."""
    if not password:
        return url
    scheme, rest = url.split("://", 1)
    return "{}://{}:{}@{}".format(
        scheme, quote(user, safe=""), quote(password, safe=""), rest
    )


def _start_bridge(argv):
    try:
        child = subprocess.Popen(
            argv,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        die("cannot start web bridge: {}".format(exc))

    try:
        line = child.stderr.readline() if child.stderr else ""
        if not line:
            die("web bridge exited before reporting its local URL")
        try:
            ready = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            die("web bridge returned an invalid ready line")
        if not isinstance(ready, dict) or ready.get("type") != "ready":
            die("web bridge did not report type=ready")
        host = ready.get("host")
        port = ready.get("port")
        url = ready.get("url")
        if (
            host != "127.0.0.1"
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not (1 <= port <= 65535)
            or url != "http://127.0.0.1:{}".format(port)
        ):
            die("web bridge returned an invalid local URL")
        threading.Thread(
            target=_forward_stderr, args=(child.stderr,), daemon=True
        ).start()
        return child, url
    except BaseException:
        _stop_bridge(child)
        raise


def _supports_web(result):
    if result.get("web_access") is True:
        return True
    capabilities = result.get("capabilities", [])
    if isinstance(capabilities, dict):
        return capabilities.get("web") is True
    return isinstance(capabilities, list) and "web-access" in capabilities


# add-pi-harness (design D11): harnesses with no web UI. `sch web` is
# opencode-only; the dashboard shows `n/a` for every entry here.
_NO_WEB_UI = ("claude", "pi")
# Harnesses with an ACP agent in the microVM — the only ones for which
# `sch acp` is a usable alternative.
_HAS_ACP_AGENT = ("opencode", "claude")


def _web_alternatives(harness, ws):
    if harness in _HAS_ACP_AGENT:
        return "'sch acp {}', 'sch shell {}', or 'sch run {}'".format(ws, ws, ws)
    return "'sch shell {}' or 'sch run {}'".format(ws, ws)


def cmd_web(cfg, args):
    ws, harness_flag, storage_flag, no_browser = _parse_args(args)

    # This rejection must precede dependency checks, workspace creation, and
    # every runtime invocation. Stated as a RULE — "harness without a web UI" —
    # rather than naming claude (add-pi-harness design D11).
    if harness_flag in _NO_WEB_UI:
        die(
            "'sch web' requires harness='opencode'; {} has no web UI — try {}".format(
                harness_flag, _web_alternatives(harness_flag, ws)
            )
        )

    node_bin = _require_local_bridge()
    tunnel_dir = repo.tunnel_dir()
    resolved = harness_mod.resolve_harness(
        cfg, ws, harness_flag or "opencode", storage_flag, provisioning=True
    )
    sid = resolved.sid
    harness = resolved.harness
    storage_backend = resolved.storage
    session_epoch = resolved.epoch
    runtime_workspace = resolved.identity or ws
    if harness != "opencode":
        die(
            "workspace '{}' is bound to harness='{}'; {} has no web UI — try "
            "{}".format(ws, harness, harness, _web_alternatives(harness, ws))
        )

    arn = runtime_arn(cfg)
    hint = "fresh" if resolved.was_created else "resumed"
    print(
        "sch: provisioning microVM (storage hint: {}, harness: {})...".format(
            hint, harness
        ),
        file=sys.stderr,
    )
    warmup = runtime.invoke_verified(
        cfg,
        sid,
        runtime.payload_noop(
            runtime_workspace, harness, hint, storage_backend, session_epoch
        ),
        "web-warmup",
    )
    storage_error = runtime.storage_verification_error(warmup, storage_backend)
    if storage_error:
        die(storage_error)

    remote_port = ""
    remote_password = ""
    serve_status = ""
    attempt = 0
    while attempt < _SERVE_ENSURE_ATTEMPTS:
        attempt += 1
        result = runtime.invoke_verified(
            cfg,
            sid,
            runtime.payload_serve_ensure(
                runtime_workspace, harness, storage_backend, session_epoch
            ),
            "serve-ensure",
        )
        if result.ok:
            serve_status = result.get("status", "unknown")
            if not _supports_web(result):
                die("runtime image predates web access; deploy an updated runtime image")
            if serve_status == "ok":
                remote_port = str(result.get("port", "") or "")
                remote_password = str(
                    ((result.get("auth") or {}).get("password") or "")
                )
                if remote_port:
                    break
        else:
            serve_status = "invoke-failed"
        if attempt < _SERVE_ENSURE_ATTEMPTS:
            time.sleep(_SERVE_ENSURE_RETRY_DELAY_S)
    if not (serve_status == "ok" and remote_port):
        die(
            "serve-ensure did not report status=ok with a port after {} attempts "
            "(last status: {})".format(attempt, serve_status)
        )

    bridge_argv = [
        node_bin,
        str(tunnel_dir / "web.js"),
        "--region",
        cfg.region,
        "--runtime-arn",
        arn,
        "--session-id",
        sid,
        "--workspace",
        runtime_workspace,
        "--storage",
        storage_backend,
        "--session-epoch",
        str(session_epoch),
        "--remote-port",
        remote_port,
    ]
    child, url = _start_bridge(bridge_argv)
    # OpenCode 2 `serve` requires basic auth (user "opencode"): the printed URL
    # carries the credentials so the browser and dashboard launchers open the
    # UI without a prompt. The bridge listens on 127.0.0.1 only and the
    # password is per-microVM, minted by the shim (see serve-ensure).
    url = _with_basic_auth(url, "opencode", remote_password)
    active = False
    try:
        runtime.invoke_best_effort(
            cfg,
            sid,
            runtime.payload_mark_interactive(
                runtime_workspace, harness, True, storage_backend, session_epoch
            ),
        )
        active = True
        workspace.mark_status(cfg, ws, "web-opened")
        # Dashboard launchers read this ready line from a pipe while this
        # process remains alive, so it must not wait for block buffering.
        print(url, flush=True)
        if not no_browser:
            try:
                if not webbrowser.open(url):
                    print("sch: could not open browser automatically", file=sys.stderr)
            except Exception as exc:
                print("sch: could not open browser: {}".format(exc), file=sys.stderr)
        try:
            return child.wait()
        except KeyboardInterrupt:
            return 130
    finally:
        _stop_bridge(child)
        if active:
            runtime.invoke_best_effort(
                cfg,
                sid,
                runtime.payload_mark_interactive(
                    runtime_workspace, harness, False, storage_backend, session_epoch
                ),
            )
            workspace.mark_status(cfg, ws, "web-closed")
