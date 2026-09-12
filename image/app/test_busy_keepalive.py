"""Offline tests for the interactive busy keep-alive watcher
(add-interactive-busy-keepalive): the shim must advertise HealthyBusy while
any opencode process shows fresh "busy" activity markers, release on clean
idle, release+notify on staleness (process died mid-turn) and on the max-hold
cap (bounded billing), and never double-hold or re-hold a capped episode."""

from __future__ import annotations

import __future__
import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
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


def load_main():
    app_dir = Path(__file__).parent
    sys.path.insert(0, str(app_dir))
    bedrock = types.ModuleType("bedrock_agentcore")
    bedrock.BedrockAgentCoreApp = FakeApp
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_args, **_kwargs: None
    module_name = "sch_busy_keepalive_main"
    spec = importlib.util.spec_from_file_location(module_name, app_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    with patch.dict(
        sys.modules,
        {module_name: module, "bedrock_agentcore": bedrock, "boto3": boto3},
    ), patch("threading.Thread"):
        source = (app_dir / "main.py").read_text(encoding="utf-8")
        code = compile(
            source,
            str(app_dir / "main.py"),
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
        )
        exec(code, module.__dict__)
    return module


main = load_main()

# Hermetic spool dir (add-task-liveness-safety, task 4.4): _telegram_notify
# now falls back to the notifier's spool dir when the singleton does not exist
# yet, so a suite running inside a real microVM (where SCH_TELEGRAM_* IS set)
# would otherwise drop events into the LIVE shim's spool and have them sent
# for real. Redirected for the whole module; nothing here asserts on it.
_SPOOL_TMP = None
_SPOOL_SAVED = None


def setUpModule():
    global _SPOOL_TMP, _SPOOL_SAVED  # noqa: PLW0603
    _SPOOL_TMP = tempfile.TemporaryDirectory()
    _SPOOL_SAVED = main.telegram_notifier.SPOOL_DIR
    main.telegram_notifier.SPOOL_DIR = Path(_SPOOL_TMP.name) / "spool"


def tearDownModule():
    main.telegram_notifier.SPOOL_DIR = _SPOOL_SAVED
    _SPOOL_TMP.cleanup()


class RecorderApp:
    """Records the advisory async-task calls the watcher makes."""

    def __init__(self):
        self.added = []
        self.completed = []
        self._seq = 0

    def add_async_task(self, name, meta=None):
        self._seq += 1
        handle = f"handle-{self._seq}"
        self.added.append((name, meta, handle))
        return handle

    def complete_async_task(self, handle):
        self.completed.append(handle)


class FakeNotifier:
    def __init__(self):
        self.events = []

    def notify(self, etype, payload, workspace=None):
        self.events.append((etype, payload, workspace))


T0 = 1_000_000.0


class BusyKeepaliveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.activity_dir = Path(self.tmp.name)
        self._saved_dir = main.ACTIVITY_DIR
        main.ACTIVITY_DIR = self.activity_dir
        self.addCleanup(lambda: setattr(main, "ACTIVITY_DIR", self._saved_dir))
        self.app = RecorderApp()
        self._saved_app = main.app
        main.app = self.app
        self.addCleanup(lambda: setattr(main, "app", self._saved_app))
        self.notifier = FakeNotifier()
        main._TELEGRAM["notifier"] = self.notifier
        self.addCleanup(lambda: main._TELEGRAM.update({"notifier": None}))
        main._BUSY_STATE.update({
            "handle": None, "since": None, "capped": False,
            "sessions": [], "last_release_reason": None,
        })

    def _write_marker(self, session, state, ts):
        (self.activity_dir / f"{session}.json").write_text(
            json.dumps({"sessionID": session, "state": state, "ts": ts}),
            encoding="utf-8",
        )

    def test_fresh_busy_engages_hold_once(self):
        self._write_marker("s1", "busy", T0)
        result = main._busy_keepalive_tick(now=T0 + 1)
        self.assertTrue(result["active"])
        self.assertEqual(len(self.app.added), 1)
        self.assertEqual(self.app.added[0][0], "interactive_activity")
        # Second tick with the same fresh marker: no duplicate hold.
        main._busy_keepalive_tick(now=T0 + 2)
        self.assertEqual(len(self.app.added), 1)
        self.assertEqual(self.app.completed, [])

    def test_clean_idle_releases_without_notification(self):
        self._write_marker("s1", "busy", T0)
        main._busy_keepalive_tick(now=T0 + 1)
        self._write_marker("s1", "idle", T0 + 5)
        result = main._busy_keepalive_tick(now=T0 + 6)
        self.assertFalse(result["active"])
        self.assertEqual(self.app.completed, [self.app.added[0][2]])
        self.assertEqual(self.notifier.events, [])
        self.assertEqual(main._BUSY_STATE["last_release_reason"], "idle")

    def test_stale_marker_releases_and_notifies(self):
        self._write_marker("s1", "busy", T0)
        main._busy_keepalive_tick(now=T0 + 1)
        # No further activity: the marker crosses the staleness threshold
        # without a session.idle (writer process died mid-turn).
        late = T0 + main.BUSY_STALE_SECONDS + 30
        result = main._busy_keepalive_tick(now=late)
        self.assertFalse(result["active"])
        self.assertEqual(len(self.app.completed), 1)
        self.assertEqual(len(self.notifier.events), 1)
        etype, payload, _ws = self.notifier.events[0]
        self.assertEqual(etype, "busy-stale")
        self.assertGreaterEqual(payload["quiet_s"], main.BUSY_STALE_SECONDS)
        self.assertEqual(main._BUSY_STATE["last_release_reason"], "stale")

    def test_no_hold_without_busy_markers(self):
        self._write_marker("s1", "idle", T0)
        result = main._busy_keepalive_tick(now=T0 + 1)
        self.assertFalse(result["active"])
        self.assertEqual(self.app.added, [])
        self.assertEqual(self.app.completed, [])

    def test_cap_releases_notifies_and_blocks_rehold_until_idle(self):
        self._write_marker("s1", "busy", T0)
        main._busy_keepalive_tick(now=T0 + 1)
        # Keep the marker fresh past the cap.
        capped_at = T0 + main.BUSY_MAX_HOLD_SECONDS + 60
        self._write_marker("s1", "busy", capped_at - 5)
        result = main._busy_keepalive_tick(now=capped_at)
        self.assertFalse(result["active"])
        self.assertTrue(result["capped"])
        self.assertEqual(len(self.notifier.events), 1)
        etype, payload, _ws = self.notifier.events[0]
        self.assertEqual(etype, "busy-cap")
        self.assertGreaterEqual(payload["held_s"], main.BUSY_MAX_HOLD_SECONDS)
        # Still-busy episode: the capped flag blocks a re-hold.
        self._write_marker("s1", "busy", capped_at + 5)
        result = main._busy_keepalive_tick(now=capped_at + 10)
        self.assertFalse(result["active"])
        self.assertEqual(len(self.app.added), 1)
        # Episode ends (idle) -> cap re-armed -> a NEW turn holds again.
        self._write_marker("s1", "idle", capped_at + 20)
        main._busy_keepalive_tick(now=capped_at + 25)
        self.assertFalse(main._BUSY_STATE["capped"])
        self._write_marker("s1", "busy", capped_at + 30)
        result = main._busy_keepalive_tick(now=capped_at + 31)
        self.assertTrue(result["active"])
        self.assertEqual(len(self.app.added), 2)

    def test_multiple_sessions_release_only_when_all_quiet(self):
        self._write_marker("s1", "busy", T0)
        self._write_marker("s2", "busy", T0)
        main._busy_keepalive_tick(now=T0 + 1)
        self.assertEqual(len(self.app.added), 1)
        self._write_marker("s1", "idle", T0 + 5)
        result = main._busy_keepalive_tick(now=T0 + 6)
        self.assertTrue(result["active"])  # s2 still busy
        self.assertEqual(result["sessions"], ["s2"])
        self._write_marker("s2", "idle", T0 + 8)
        result = main._busy_keepalive_tick(now=T0 + 9)
        self.assertFalse(result["active"])
        self.assertEqual(len(self.app.completed), 1)

    def test_gc_unlinks_ancient_markers(self):
        ancient = T0 - main.BUSY_GC_SECONDS - 100
        self._write_marker("old", "idle", ancient)
        main._busy_keepalive_tick(now=T0)
        self.assertFalse((self.activity_dir / "old.json").exists())

    def test_advisory_failures_never_raise(self):
        class BrokenApp:
            def add_async_task(self, *_a, **_k):
                raise RuntimeError("sdk down")

            def complete_async_task(self, *_a, **_k):
                raise RuntimeError("sdk down")

        main.app = BrokenApp()
        self._write_marker("s1", "busy", T0)
        result = main._busy_keepalive_tick(now=T0 + 1)  # must not raise
        self.assertFalse(result["active"])

    def test_tick_without_notifier_is_silent(self):
        main._TELEGRAM["notifier"] = None
        self._write_marker("s1", "busy", T0)
        main._busy_keepalive_tick(now=T0 + 1)
        late = T0 + main.BUSY_STALE_SECONDS + 30
        main._busy_keepalive_tick(now=late)  # stale release, no notifier
        self.assertEqual(main._BUSY_STATE["handle"], None)


if __name__ == "__main__":
    unittest.main()
