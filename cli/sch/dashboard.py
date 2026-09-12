"""Read-only data aggregation and refresh scheduling for the dashboard."""

import json
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional, Tuple

from .commands.list import read_workspace_records
from .commands.status import read_offline_status
from .config import checkpoint_bucket


DEFAULT_REFRESH_INTERVAL = 20.0

# A live microVM's checkpoint loop rewrites the workspace manifest roughly
# every 60s (SCH_CHECKPOINT_INTERVAL); a manifest older than 3 intervals means
# the microVM is almost certainly gone (idle timeout / stop / max lifetime).
# AgentCore Runtime (Preview) has no ListRuntimeSessions API, so manifest
# freshness is the best PASSIVE liveness signal available.
LIVE_THRESHOLD_S = 180.0


@dataclass(frozen=True)
class WorkspaceSnapshot:
    name: str
    harness: str
    storage: str
    task_state: str
    heartbeat: Optional[str]
    checkpoint: Optional[str]
    status_json: str
    error: str = ""
    manifest_age_s: Optional[float] = None


@dataclass(frozen=True)
class DashboardSnapshot:
    workspaces: Tuple[WorkspaceSnapshot, ...]
    refreshed_at: float


def read_manifest_age(cfg, runtime_workspace, clock=time.time):
    """Age in seconds of the workspace's L2 manifest, or None if unreadable.

    Passive liveness probe: HeadObject on ``checkpoints/<ws>/manifest.json``
    only — it never invokes AgentCore and can never wake a microVM (same
    read-only contract as :func:`read_offline_status`).
    """
    try:
        bucket = checkpoint_bucket(cfg)
        result = subprocess.run(
            [
                "aws", "s3api", "head-object", "--bucket", bucket,
                "--key", "checkpoints/{}/manifest.json".format(runtime_workspace),
                "--region", cfg.region,
                "--query", "LastModified", "--output", "text",
            ],
            capture_output=True,
            text=True,
        )
    except (OSError, ValueError, RuntimeError, SystemExit):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    # The CLI renders LastModified per `cli_timestamp_format`: iso8601
    # (default) or the HTTP wire format on legacy configurations.
    try:
        modified = datetime.fromisoformat(raw)
    except ValueError:
        try:
            modified = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    if modified.tzinfo is None:
        return None
    return max(0.0, clock() - modified.timestamp())


def _read_workspace_snapshot(cfg, record, clock=time.time):
    # Liveness first and independently: a workspace with no persisted task
    # status (never ran a task) can still have a live microVM, and vice versa.
    manifest_age_s = read_manifest_age(cfg, record.runtime_workspace, clock)
    try:
        raw = read_offline_status(cfg, record.runtime_workspace, require_success=True)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("task status is not an object")
    except Exception as exc:
        return _unknown_workspace_snapshot(record, exc, manifest_age_s)
    return WorkspaceSnapshot(
        name=record.name,
        harness=record.harness,
        storage=record.storage,
        task_state=str(data.get("state") or "none"),
        heartbeat=data.get("heartbeat_utc") or data.get("finished_utc"),
        checkpoint=data.get("checkpoint_status"),
        status_json=raw,
        manifest_age_s=manifest_age_s,
    )


def _unknown_workspace_snapshot(record, error, manifest_age_s=None):
    return WorkspaceSnapshot(
        name=record.name,
        harness=record.harness,
        storage=record.storage,
        task_state="unknown",
        heartbeat=None,
        checkpoint=None,
        status_json='{"state":"unknown"}',
        error=str(error) or "task status is unavailable",
        manifest_age_s=manifest_age_s,
    )


def aggregate_snapshot(cfg, records=None, max_workers=None, clock=time.time):
    """Build a name-sorted snapshot using only index/registry and S3 reads."""
    source = tuple(read_workspace_records(cfg) if records is None else records)
    if not source:
        return DashboardSnapshot((), clock())

    workers = max_workers or min(32, len(source))
    snapshots = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_read_workspace_snapshot, cfg, record, clock): record
            for record in source
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                snapshots.append(future.result())
            except Exception as exc:
                snapshots.append(_unknown_workspace_snapshot(record, exc))
    snapshots.sort(key=lambda item: item.name)
    return DashboardSnapshot(tuple(snapshots), clock())


class RefreshController:
    """Publish periodic and manually-triggered snapshots from one thread."""

    def __init__(self, cfg, interval=DEFAULT_REFRESH_INTERVAL, output=None, aggregate=None):
        if interval <= 0:
            raise ValueError("refresh interval must be greater than zero")
        self.cfg = cfg
        self.interval = float(interval)
        self.output = output if output is not None else queue.Queue()
        self._aggregate = aggregate or aggregate_snapshot
        self._refresh = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="sch-dashboard-refresh", daemon=True
        )
        self._thread.start()

    def trigger(self):
        self._refresh.set()

    def stop(self, timeout=None):
        self._stop.set()
        self._refresh.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            try:
                update = self._aggregate(self.cfg)
            except Exception as exc:
                update = exc
            if self._stop.is_set():
                break
            self.output.put(update)
            self._refresh.wait(self.interval)
            self._refresh.clear()
