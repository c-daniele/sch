"""Offline tests for workspace fs-sync exclusivity."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fs_sync_lease import FsSyncLeaseRegistry  # noqa: E402


leases = FsSyncLeaseRegistry()
assert leases.acquire("myws", "tunnel-a")
assert leases.acquire("myws", "tunnel-a")  # reconnect is allowed
assert not leases.acquire("myws", "tunnel-b")
assert leases.owner("myws") == "tunnel-a"
leases.release("myws", "tunnel-b")  # a non-owner cannot clear the lease
assert leases.owner("myws") == "tunnel-a"
leases.release("myws", "tunnel-a")  # normal close / orphan sweep cleanup
assert leases.owner("myws") is None
assert leases.acquire("myws", "tunnel-b")
print("test_fs_sync_lease.py: ALL PASS")
