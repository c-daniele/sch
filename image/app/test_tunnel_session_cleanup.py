"""Offline regression test for cancellation-safe tunnel lease cleanup."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch


class FakeApp:
    def entrypoint(self, func):
        return func

    def websocket(self, func):
        return func

    def add_async_task(self, *_args, **_kwargs):
        return object()

    def complete_async_task(self, *_args, **_kwargs):
        return None

    def run(self, *_args, **_kwargs):
        return None


app_dir = Path(__file__).parent
sys.path.insert(0, str(app_dir))
bedrock = types.ModuleType("bedrock_agentcore")
bedrock.BedrockAgentCoreApp = FakeApp
sys.modules["bedrock_agentcore"] = bedrock
boto3 = types.ModuleType("boto3")
boto3.client = lambda *_args, **_kwargs: None
sys.modules["boto3"] = boto3

spec = importlib.util.spec_from_file_location("sch_tunnel_cleanup_main", app_dir / "main.py")
main = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = main
with patch("threading.Thread"):
    spec.loader.exec_module(main)


class FakeReader:
    async def read(self, _size):
        return b""


class FakeWriter:
    def write(self, _chunk):
        return None


class FakeProcess:
    def __init__(self):
        self.terminated = asyncio.Event()
        self.finish = asyncio.Event()
        self.killed = False

    def terminate(self):
        self.terminated.set()

    async def wait(self):
        await self.finish.wait()
        return 0

    def kill(self):
        self.killed = True


class FakeLog:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


async def test_cancelled_caller_does_not_strand_lease():
    main._TUNNEL_SESSIONS.clear()
    main._FS_WORKSPACE_LEASES = main.FsSyncLeaseRegistry()
    process = FakeProcess()
    stderr_log = FakeLog()
    session = main._TunnelSession(
        "tunnel-a", "fs", FakeReader(), FakeWriter(), process, stderr_log, "myws"
    )
    main._TUNNEL_SESSIONS[session.tunnel_id] = session
    assert main._FS_WORKSPACE_LEASES.acquire("myws", session.tunnel_id)

    caller = asyncio.create_task(session.terminate())
    await process.terminated.wait()
    caller.cancel()
    try:
        await caller
    except asyncio.CancelledError:
        pass

    assert main._FS_WORKSPACE_LEASES.owner("myws") == "tunnel-a"
    process.finish.set()
    await session._terminate_task

    assert main._FS_WORKSPACE_LEASES.owner("myws") is None
    assert "tunnel-a" not in main._TUNNEL_SESSIONS
    assert stderr_log.closed
    assert not process.killed


asyncio.run(test_cancelled_caller_does_not_strand_lease())
print("test_tunnel_session_cleanup.py: ALL PASS")
